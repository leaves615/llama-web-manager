import datetime as dt
import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


APP_ROOT = Path(__file__).parent
DAEMON_PID_FILE = APP_ROOT / "daemon.pid"


def _get_daemon_info() -> Optional[tuple]:
    """读取 daemon PID 和端口"""
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        with open(DAEMON_PID_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            return None
        try:
            pid = int(lines[0].strip())
            port = int(lines[1].strip())
            return pid, port
        except ValueError:
            return None
    except Exception:
        return None


def _is_daemon_running() -> bool:
    """检查 daemon 是否运行（结合PID和TCP端口验证）"""
    info = _get_daemon_info()
    if not info:
        return False
    
    pid, port = info
    
    # 检查PID对应的进程是否存在
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            try:
                DAEMON_PID_FILE.unlink()
            except:
                pass
            return False
    except psutil.NoSuchProcess:
        try:
            DAEMON_PID_FILE.unlink()
        except:
            pass
        return False
    
    # 验证TCP端口连通性
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
            if result == 0:
                return True
        try:
            DAEMON_PID_FILE.unlink()
        except Exception:
            pass
        return False
    except Exception:
        try:
            DAEMON_PID_FILE.unlink()
        except Exception:
            pass
        return False


def _control_request(request: Dict, timeout: float = 10) -> Optional[Dict]:
    """向 daemon 发送 TCP 控制请求"""
    info = _get_daemon_info()
    if not info:
        return None

    daemon_pid, port = info

    try:
        proc = psutil.Process(daemon_pid)
        if not proc.is_running():
            return None
    except psutil.NoSuchProcess:
        return None

    # 简单重试机制以提高鲁棒性
    attempts = 3
    for attempt in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(("127.0.0.1", port))
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                response = sock.recv(8192)
                return json.loads(response.decode("utf-8"))
        except Exception as e:
            if attempt == attempts - 1:
                print(f"TCP request failed: {e}")
                return None
            time.sleep(0.2 * (attempt + 1))


def stop_daemon_via_tcp() -> bool:
    """通过 TCP 连接停止 daemon，并等待其真正退出"""
    response = _control_request({"action": "stop", "target": "daemon"}, timeout=10)
    if response and response.get("success"):
        if _wait_for_daemon_stopped(timeout=10):
            return True
    return False


def _start_daemon_process() -> None:
    daemon_path = APP_ROOT / "daemon.py"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, str(daemon_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=True,
    )


def _wait_for_daemon_ready(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_daemon_running():
            return True
        time.sleep(0.5)
    return False


def _wait_for_daemon_stopped(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_daemon_running():
            return True
        time.sleep(0.5)
    return False
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
import yaml


APP_ROOT = Path(__file__).parent
LOG_DIR = APP_ROOT / "logs"
DB_FILE = APP_ROOT / "instances.db"
CONFIG_FILE = APP_ROOT / "config.yaml"
LOG_DIR.mkdir(parents=True, exist_ok=True)


APP_ROOT = Path(__file__).parent
LOG_DIR = APP_ROOT / "logs"
DB_FILE = APP_ROOT / "instances.db"
CONFIG_FILE = APP_ROOT / "config.yaml"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def load_runtime_config() -> Dict:
    default_cfg = {
        "scan_roots": [],
        "scan_max_depth": 5,
        "scan_interval_seconds": 30,
        "model_scan_roots": [],
        "model_scan_max_depth": 5,
        "model_extensions": [".gguf", ".bin"],
        "param_sync_enabled": False,
        "param_sync_interval_seconds": 86400,
        "param_cache_file": ".cache/llama_params.json",
        "param_readme_url": "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md",
    }
    if not CONFIG_FILE.exists():
        return default_cfg

    try:
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_cfg
        roots = raw.get("scan_roots", [])
        depth = raw.get("scan_max_depth", 5)
        scan_interval = raw.get("scan_interval_seconds", 30)
        model_roots = raw.get("model_scan_roots", roots)
        model_depth = raw.get("model_scan_max_depth", depth)
        model_exts = raw.get("model_extensions", [".gguf", ".bin"])
        param_sync_enabled = raw.get("param_sync_enabled", False)
        param_sync_interval = raw.get("param_sync_interval_seconds", 86400)
        param_cache_file = raw.get("param_cache_file", ".cache/llama_params.json")
        param_readme_url = raw.get(
            "param_readme_url",
            "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md",
        )
        if not isinstance(roots, list):
            roots = []
        if not isinstance(depth, int) or depth <= 0:
            depth = 5
        if not isinstance(scan_interval, int) or scan_interval <= 0:
            scan_interval = 30
        if not isinstance(model_roots, list):
            model_roots = []
        if not isinstance(model_depth, int) or model_depth <= 0:
            model_depth = depth
        if not isinstance(model_exts, list) or not model_exts:
            model_exts = [".gguf", ".bin"]
        if not isinstance(param_sync_enabled, bool):
            param_sync_enabled = str(param_sync_enabled).strip().lower() in {"1", "true", "yes", "on", "enabled"}
        if not isinstance(param_sync_interval, int) or param_sync_interval <= 0:
            param_sync_interval = 86400
        param_cache_file = str(param_cache_file).strip() or ".cache/llama_params.json"
        param_readme_url = str(param_readme_url).strip() or "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md"

        normalized_exts = []
        for ext in model_exts:
            text = str(ext).strip().lower()
            if not text:
                continue
            if not text.startswith("."):
                text = f".{text}"
            normalized_exts.append(text)

        return {
            "scan_roots": [str(x).strip() for x in roots if str(x).strip()],
            "scan_max_depth": depth,
            "scan_interval_seconds": scan_interval,
            "model_scan_roots": [str(x).strip() for x in model_roots if str(x).strip()],
            "model_scan_max_depth": model_depth,
            "model_extensions": sorted(set(normalized_exts)),
            "param_sync_enabled": param_sync_enabled,
            "param_sync_interval_seconds": param_sync_interval,
            "param_cache_file": param_cache_file,
            "param_readme_url": param_readme_url,
        }
    except Exception:
        return default_cfg


class InstanceRecord:
    def __init__(
        self,
        instance_id: str,
        name: str,
        executable_path: str,
        command: List[str],
        process: subprocess.Popen,
        log_file: Path,
        visual_args: Dict,
        freeform_args: str,
        env_vars: List[Dict],
        created_at: str | None = None,
        is_attached: bool = False,
    ) -> None:
        self.instance_id = instance_id
        self.name = name
        self.executable_path = executable_path
        self.command = command
        self.process = process
        self.log_file = log_file
        self.visual_args = visual_args
        self.freeform_args = freeform_args
        self.env_vars = env_vars
        self.created_at = created_at or utc_now_iso()
        self.stopped_by_manager = False
        self.logs = deque(maxlen=3000)
        self._lock = threading.Lock()
        self._log_reader_thread: threading.Thread | None = None

    def append_log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)

    def get_logs(self, lines: int = 200) -> List[str]:
        with self._lock:
            if lines <= 0:
                return []
            return list(self.logs)[-lines:]

    @property
    def pid(self) -> int:
        if self.process:
            return self.process.pid
        ps_proc = getattr(self, "_ps_process", None)
        if ps_proc:
            return ps_proc.pid
        return 0

    @property
    def status(self) -> str:
        if self.process:
            return "running" if self.process.poll() is None else f"exited({self.process.returncode})"
        ps_proc = getattr(self, "_ps_process", None)
        if ps_proc:
            try:
                if ps_proc.is_running():
                    return "running"
                return "stopped"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return "stopped"
        return "stopped"

    def set_log_reader_thread(self, thread: threading.Thread | None) -> None:
        self._log_reader_thread = thread


class InstanceManager:
    def __init__(self) -> None:
        self.instances: Dict[str, InstanceRecord] = {}
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._init_db()

    def _db_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

    def _db_execute(self, sql: str, params=()):
        with self._db_lock:
            conn = self._db_conn()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            conn.close()

    def _init_db(self) -> None:
        with self._db_lock:
            with self._db_conn() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
                )
                row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC").fetchone()
                current_version = row["version"] if row else 0
                if current_version < 1:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS instances (
                            instance_id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            executable_path TEXT NOT NULL,
                            command_json TEXT NOT NULL,
                            visual_args_json TEXT NOT NULL,
                            freeform_args TEXT NOT NULL,
                            env_vars_json TEXT NOT NULL DEFAULT '[]',
                            log_file TEXT NOT NULL,
                            pid INTEGER,
                            status TEXT NOT NULL,
                            command TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")
                    current_version = 1
                if current_version < 2:
                    try:
                        conn.execute("ALTER TABLE instances ADD COLUMN command TEXT")
                    except sqlite3.OperationalError:
                        pass
                    conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")

    def _upsert_instance(
        self,
        *,
        instance_id: str,
        name: str,
        executable_path: str,
        command: List[str],
        visual_args: Dict,
        freeform_args: str,
        env_vars: List[Dict],
        log_file: Path,
        pid: int | None,
        status: str,
        created_at: str,
    ) -> None:
        now = utc_now_iso()
        with self._db_lock:
            with self._db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO instances (
                        instance_id, name, executable_path, command_json, visual_args_json,
                        freeform_args, env_vars_json, log_file, pid, status, command, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                        name=excluded.name,
                        executable_path=excluded.executable_path,
                        command_json=excluded.command_json,
                        visual_args_json=excluded.visual_args_json,
                        freeform_args=excluded.freeform_args,
                        env_vars_json=excluded.env_vars_json,
                        log_file=excluded.log_file,
                        pid=excluded.pid,
                        status=excluded.status,
                        command=excluded.command,
                        updated_at=excluded.updated_at
                    """,
                    (
                        instance_id,
                        name,
                        executable_path,
                        json.dumps(command, ensure_ascii=False),
                        json.dumps(visual_args, ensure_ascii=False),
                        freeform_args,
                        json.dumps(env_vars, ensure_ascii=False),
                        str(log_file),
                        pid,
                        status,
                        json.dumps(command, ensure_ascii=False),
                        created_at,
                        now,
                    ),
                )

    def _update_instance_status(self, instance_id: str, pid: int | None, status: str) -> None:
        with self._db_lock:
            with self._db_conn() as conn:
                conn.execute(
                    "UPDATE instances SET pid = ?, status = ?, updated_at = ? WHERE instance_id = ?",
                    (pid, status, utc_now_iso(), instance_id),
                )

    def _list_persisted_instances(self) -> List[Dict]:
        # 首先检查 daemon 是否还在运行，如果不运行则清理所有running状态的实例
        if not _is_daemon_running():
            with self._db_lock:
                with self._db_conn() as conn:
                    conn.execute(
                        "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE status = 'running'",
                        (utc_now_iso(),),
                    )
        
        with self._db_lock:
            with self._db_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM instances ORDER BY datetime(updated_at) DESC"
                ).fetchall()

        items: List[Dict] = []
        for row in rows:
            # 验证进程状态：如果数据库显示running但进程不存在，则更新为exited
            status = row["status"]
            pid = row["pid"]
            instance_id = row["instance_id"]
            
            if status == "running" and pid:
                try:
                    proc = psutil.Process(pid)
                    if not proc.is_running():
                        # 进程已结束，更新数据库
                        with self._db_lock:
                            with self._db_conn() as conn:
                                conn.execute(
                                    "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                                    (utc_now_iso(), instance_id),
                                )
                        status = "exited"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # 进程不存在，更新数据库
                    with self._db_lock:
                        with self._db_conn() as conn:
                            conn.execute(
                                "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                                (utc_now_iso(), instance_id),
                            )
                    status = "exited"
            
            items.append(
                {
                    "instance_id": row["instance_id"],
                    "name": row["name"],
                    "executable_path": row["executable_path"],
                    "pid": row["pid"],
                    "status": status,
                    "created_at": row["created_at"],
                    "command": json.loads(row["command_json"]),
                    "log_file": row["log_file"],
                    "visual_args": json.loads(row["visual_args_json"]),
                    "freeform_args": row["freeform_args"],
                    "env_vars": json.loads(row["env_vars_json"] or "[]"),
                }
            )
        return items

    def _get_persisted_instance(self, instance_id: str) -> Dict | None:
        with self._db_lock:
            with self._db_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM instances WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()

        if not row:
            return None

        return {
            "instance_id": row["instance_id"],
            "name": row["name"],
            "executable_path": row["executable_path"],
            "pid": row["pid"],
            "status": row["status"],
            "created_at": row["created_at"],
            "command": json.loads(row["command_json"]),
            "log_file": row["log_file"],
            "visual_args": json.loads(row["visual_args_json"]),
            "freeform_args": row["freeform_args"],
            "env_vars": json.loads(row["env_vars_json"] or "[]"),
        }

    def read_log_file(self, instance_id: str, lines: int = 200) -> List[str]:
        persisted = self._get_persisted_instance(instance_id)
        if not persisted:
            return []

        log_file = Path(persisted["log_file"])
        if not log_file.exists():
            return []

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                return [line.rstrip("\n\r") for line in all_lines[-lines:] if line.strip()]
        except Exception:
            return []

    def _start_process(self, command: List[str], env_vars: List[Dict] | None = None) -> subprocess.Popen:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        env = os.environ.copy()
        if env_vars:
            for item in env_vars:
                enabled_raw = item.get("enabled", True)
                enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).strip().lower() not in {
                    "0",
                    "false",
                    "off",
                    "no",
                }
                if not enabled:
                    continue
                key = (item.get("key") or "").strip()
                value = (item.get("value") or "").strip()
                if key:
                    env[key] = value

        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )

    def _terminate_process(self, record: InstanceRecord) -> None:
        record.stopped_by_manager = True
        if record.process:
            if record.process.poll() is None:
                record.process.terminate()
                try:
                    record.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    record.process.kill()
        else:
            ps_proc = getattr(record, "_ps_process", None)
            if ps_proc:
                try:
                    proc = psutil.Process(ps_proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except psutil.TimeoutExpired:
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def _read_log_file_tail(self, file_path: str, lines: int) -> List[str]:
        path = Path(file_path)
        if not path.exists() or lines <= 0:
            return []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-lines:]]

    def _read_log_file_from(self, file_path: str, offset: int, limit: int) -> List[str]:
        path = Path(file_path)
        if not path.exists() or offset < 0 or limit <= 0:
            return []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            total = len(all_lines)
            if offset >= total:
                return []
            start = max(0, offset - limit)
            return [line.rstrip("\n") for line in all_lines[start:offset]]

    def _resolve_executable(self, server_dir: str) -> str:
        raw = (server_dir or "").strip()
        if not raw:
            raise ValueError("server_dir 不能为空")

        path = Path(raw)
        if path.is_file():
            return str(path)

        if not path.is_dir():
            raise ValueError(f"未找到目录: {server_dir}")

        exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
        candidate = path / exe_name
        if not candidate.exists():
            raise ValueError(f"目录中未找到可执行文件: {candidate}")

        return str(candidate)

    def discover_llama_binaries(self, base_dir: str, max_depth: int = 4) -> List[str]:
        raw = (base_dir or "").strip()
        if not raw:
            raise ValueError("base_dir 不能为空")

        root = Path(raw)
        if not root.exists() or not root.is_dir():
            raise ValueError(f"扫描目录不存在: {base_dir}")

        def is_candidate(name: str) -> bool:
            lower = name.lower()
            if os.name == "nt":
                return lower.startswith("llama-server") and lower.endswith(".exe")
            return lower.startswith("llama-server")

        found: List[str] = []
        root_depth = len(root.parts)

        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            if depth >= max_depth:
                dirs[:] = []

            for fname in files:
                if not is_candidate(fname):
                    continue
                fpath = current_path / fname
                if os.name != "nt" and not os.access(fpath, os.X_OK):
                    continue
                found.append(str(fpath))

        # 去重并按路径排序，保证前端显示稳定。
        unique_sorted = sorted(set(found), key=lambda p: p.lower())
        return unique_sorted

    def discover_model_files(
        self,
        base_dir: str,
        extensions: List[str],
        max_depth: int = 5,
    ) -> List[str]:
        raw = (base_dir or "").strip()
        if not raw:
            raise ValueError("base_dir 不能为空")

        root = Path(raw)
        if not root.exists() or not root.is_dir():
            raise ValueError(f"扫描目录不存在: {base_dir}")

        normalized_exts = set()
        for ext in extensions:
            text = str(ext).strip().lower()
            if not text:
                continue
            if not text.startswith("."):
                text = f".{text}"
            normalized_exts.add(text)

        if not normalized_exts:
            normalized_exts = {".gguf", ".bin"}

        found: List[str] = []
        root_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            if depth >= max_depth:
                dirs[:] = []

            for fname in files:
                suffix = Path(fname).suffix.lower()
                if suffix not in normalized_exts:
                    continue
                found.append(str(current_path / fname))

        return sorted(set(found), key=lambda p: p.lower())

    def list_instances(self) -> List[Dict]:
        return self._list_persisted_instances()

    def get_instance(self, instance_id: str) -> InstanceRecord | None:
        with self._lock:
            return self.instances.get(instance_id)

    def create_instance(
        self,
        name: str,
        server_dir: str,
        visual_args: Dict,
        freeform_args: str,
        env_vars: List[Dict],
    ) -> Dict:
        if not server_dir:
            raise ValueError("server_dir 不能为空")

        instance_id = str(uuid.uuid4())[:8]
        command = self._build_command(server_dir, visual_args, freeform_args)
        log_file = LOG_DIR / f"{instance_id}.log"
        created_at = utc_now_iso()

        self._upsert_instance(
            instance_id=instance_id,
            name=name or f"llama-{instance_id}",
            executable_path=server_dir,
            command=command,
            visual_args=visual_args,
            freeform_args=freeform_args,
            env_vars=env_vars,
            log_file=log_file,
            pid=None,
            status="stopped",
            created_at=created_at,
        )

        return {
            "instance_id": instance_id,
            "name": name or f"llama-{instance_id}",
            "executable_path": server_dir,
            "command": command,
            "pid": None,
            "status": "stopped",
            "created_at": created_at,
            "log_file": str(log_file),
            "visual_args": visual_args,
            "freeform_args": freeform_args,
            "env_vars": env_vars,
        }

    def start_instance(self, instance_id: str) -> Dict:
        persisted = self._get_persisted_instance(instance_id)
        if not persisted:
            raise ValueError("实例不存在")

        command = self._build_command(
            server_dir=persisted["executable_path"],
            visual_args=persisted["visual_args"],
            freeform_args=persisted["freeform_args"],
        )

        self._db_execute(
            "UPDATE instances SET command_json = ?, command = 'start', updated_at = ? WHERE instance_id = ?",
            (json.dumps(command, ensure_ascii=False), utc_now_iso(), instance_id),
        )

        return {
            "instance_id": instance_id,
            "name": persisted["name"],
            "executable_path": persisted["executable_path"],
            "command": command,
            "pid": persisted.get("pid"),
            "status": "starting",
            "created_at": persisted["created_at"],
            "log_file": persisted["log_file"],
            "visual_args": persisted["visual_args"],
            "freeform_args": persisted["freeform_args"],
            "env_vars": persisted.get("env_vars", []),
        }

    def update_instance(
        self,
        instance_id: str,
        name: str,
        server_dir: str,
        visual_args: Dict,
        freeform_args: str,
        env_vars: List[Dict],
    ) -> Dict:
        if not server_dir:
            raise ValueError("server_dir 不能为空")

        persisted = self._get_persisted_instance(instance_id)
        if not persisted:
            raise ValueError("实例不存在")

        command = self._build_command(server_dir, visual_args, freeform_args)
        log_file = persisted["log_file"]

        self._db_execute(
            "UPDATE instances SET name = ?, executable_path = ?, command_json = ?, visual_args_json = ?, freeform_args = ?, env_vars_json = ?, command = 'start', updated_at = ? WHERE instance_id = ?",
            (name, server_dir, json.dumps(command, ensure_ascii=False), json.dumps(visual_args, ensure_ascii=False), freeform_args, json.dumps(env_vars, ensure_ascii=False), utc_now_iso(), instance_id),
        )

        return {
            "instance_id": instance_id,
            "name": name,
            "executable_path": server_dir,
            "command": command,
            "pid": persisted.get("pid"),
            "status": "restarting",
            "created_at": persisted["created_at"],
            "log_file": log_file,
            "visual_args": visual_args,
            "freeform_args": freeform_args,
            "env_vars": env_vars,
        }

    def stop_instance(self, instance_id: str) -> Dict:
        persisted = self._get_persisted_instance(instance_id)
        if not persisted:
            raise ValueError("实例不存在")

        self._db_execute(
            "UPDATE instances SET command = 'stop', updated_at = ? WHERE instance_id = ?",
            (utc_now_iso(), instance_id),
        )

        return {
            "instance_id": instance_id,
            "name": persisted["name"],
            "status": "stopping",
            "pid": persisted.get("pid"),
        }

    def delete_instance(self, instance_id: str) -> Dict:
        persisted = self._get_persisted_instance(instance_id)
        if not persisted:
            raise ValueError("实例不存在")

        # Delete from database
        with self._db_lock:
            with self._db_conn() as conn:
                conn.execute("DELETE FROM instances WHERE instance_id = ?", (instance_id,))

        # Delete log file
        log_file = Path(persisted["log_file"])
        if log_file.exists():
            try:
                log_file.unlink()
            except Exception:
                pass

        return {
            "instance_id": instance_id,
            "name": persisted["name"],
            "status": "deleted",
        }

    def _capture_output(self, record: InstanceRecord) -> None:
        with record.log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{utc_now_iso()}] command: {' '.join(record.command)}\n")
            stdout = record.process.stdout
            if stdout is None:
                return

            stderr_output_lines = []
            for line in stdout:
                clean = line.rstrip("\n")
                msg = f"[{utc_now_iso()}] {clean}"
                record.append_log(msg)
                f.write(msg + "\n")
                f.flush()
                stderr_output_lines.append(msg)

            # 等待进程结束并获取返回码
            return_code = record.process.poll()
            if return_code is None:
                try:
                    return_code = record.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    return_code = -1
                    record.process.kill()
            
            final_status = "disabled" if record.stopped_by_manager else f"exited({return_code})"
            
            ended = f"[{utc_now_iso()}] process exited with code {return_code}, status={final_status}"
            record.append_log(ended)
            f.write(ended + "\n")
            
            if not record.stopped_by_manager and return_code != 0:
                error_msg = f"[{utc_now_iso()}] ERROR: Process exited with non-zero code {return_code}"
                record.append_log(error_msg)
                f.write(error_msg + "\n")
                if stderr_output_lines:
                    stderr_details = f"\n[{utc_now_iso()}] Process output:\n" + "\n".join(stderr_output_lines)
                    record.append_log(stderr_details)
                    f.write(stderr_details + "\n")
                    f.flush()
            else:
                f.flush()
            
            final_pid = None if record.stopped_by_manager else record.pid
            with self._lock:
                current = self.instances.get(record.instance_id)
                should_update = current is record or current is None
                if current is record:
                    self.instances.pop(record.instance_id, None)
            if should_update:
                self._update_instance_status(record.instance_id, final_pid, final_status)

    def _build_command(self, server_dir: str, visual_args: Dict, freeform_args: str) -> List[str]:
        executable = self._resolve_executable(server_dir)
        cmd: List[str] = [executable]

        model = (visual_args.get("model_path") or "").strip()
        draft_model = (visual_args.get("draft_model_path") or "").strip()
        host = (visual_args.get("host") or "").strip()
        port = visual_args.get("port")
        n_ctx = visual_args.get("n_ctx")
        n_threads = visual_args.get("n_threads")
        gpu_layers = visual_args.get("gpu_layers")
        draft_max = visual_args.get("draft_max")
        draft_min = visual_args.get("draft_min")
        extra_kv = visual_args.get("extra_flags") or []

        if model:
            cmd.extend(["--model", model])
        if draft_model:
            cmd.extend(["--model-draft", draft_model])
        if host:
            cmd.extend(["--host", host])
        if port:
            cmd.extend(["--port", str(port)])
        if n_ctx:
            cmd.extend(["--ctx-size", str(n_ctx)])
        if n_threads:
            cmd.extend(["--threads", str(n_threads)])
        if gpu_layers is not None and str(gpu_layers).strip() != "":
            cmd.extend(["--n-gpu-layers", str(gpu_layers)])
        if draft_max is not None:
            cmd.extend(["--draft-max", str(draft_max)])
        if draft_min is not None:
            cmd.extend(["--draft-min", str(draft_min)])

        for item in extra_kv:
            enabled_raw = item.get("enabled", True)
            enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).strip().lower() not in {
                "0",
                "false",
                "off",
                "no",
                "",
            }
            if not enabled:
                continue

            key = (item.get("key") or "").strip()
            if not key:
                continue
            if not key.startswith("-"):
                key = f"--{key}"
            value = (item.get("value") or "").strip()
            cmd.append(key)
            if value:
                cmd.append(value)

        if freeform_args.strip():
            cmd.extend(shlex.split(freeform_args, posix=(os.name != "nt")))

        return cmd

    def _serialize(self, record: InstanceRecord) -> Dict:
        return {
            "instance_id": record.instance_id,
            "name": record.name,
            "executable_path": record.executable_path,
            "pid": record.pid,
            "status": record.status,
            "created_at": record.created_at,
            "command": record.command,
            "log_file": str(record.log_file),
            "visual_args": record.visual_args,
            "freeform_args": record.freeform_args,
            "env_vars": record.env_vars,
        }


class AutoScanService:
    def __init__(self, manager: InstanceManager) -> None:
        self.manager = manager
        self._lock = threading.Lock()
        self._versions: List[Dict[str, str]] = []
        self._models: List[Dict[str, str]] = []
        self._version_error = ""
        self._model_error = ""
        self._version_scanned_at = ""
        self._model_scanned_at = ""
        self._started = False

    def _scan_versions(self, cfg: Dict) -> None:
        roots = cfg.get("scan_roots", [])
        max_depth = cfg.get("scan_max_depth", 5)
        found: List[str] = []

        if not roots:
            with self._lock:
                self._versions = []
                self._version_error = "未配置 scan_roots"
                self._version_scanned_at = utc_now_iso()
            return

        for root in roots:
            try:
                found.extend(self.manager.discover_llama_binaries(base_dir=root, max_depth=max_depth))
            except Exception:
                continue

        with self._lock:
            unique = sorted(set(found), key=lambda p: p.lower())
            self._versions = []
            for p in unique:
                folder_name = Path(p).parent.name or Path(p).name
                self._versions.append({"name": folder_name, "path": p})
            self._version_error = "" if self._versions else "未扫描到 llama-server 可执行文件"
            self._version_scanned_at = utc_now_iso()

    def _scan_models(self, cfg: Dict) -> None:
        roots = cfg.get("model_scan_roots", [])
        max_depth = cfg.get("model_scan_max_depth", cfg.get("scan_max_depth", 5))
        extensions = cfg.get("model_extensions", [".gguf", ".bin"])
        found: List[str] = []

        if not roots:
            with self._lock:
                self._models = []
                self._model_error = "未配置 model_scan_roots"
                self._model_scanned_at = utc_now_iso()
            return

        for root in roots:
            try:
                found.extend(
                    self.manager.discover_model_files(
                        base_dir=root,
                        extensions=extensions,
                        max_depth=max_depth,
                    )
                )
            except Exception:
                continue

        with self._lock:
            unique = sorted(set(found), key=lambda p: p.lower())
            merged: Dict[tuple, Dict] = {}
            shard_pattern = re.compile(r"^(.*?)(?:[-_])?(\d+)-of-(\d+)$", re.IGNORECASE)

            for p in unique:
                path_obj = Path(p)
                stem = path_obj.stem
                ext = path_obj.suffix.lower()
                parent = str(path_obj.parent)

                match = shard_pattern.match(stem)
                if match:
                    base_name = match.group(1).rstrip("-_")
                    part_idx = int(match.group(2))
                    total = int(match.group(3))

                    if total > 1:
                        key = (parent.lower(), base_name.lower(), ext)
                        existing = merged.get(key)
                        candidate = {
                            "name": base_name,
                            "path": p,
                            "part_idx": part_idx,
                        }
                        if not existing or part_idx < existing["part_idx"]:
                            merged[key] = candidate
                        continue

                key = (parent.lower(), stem.lower(), ext)
                if key not in merged:
                    merged[key] = {
                        "name": stem,
                        "path": p,
                        "part_idx": 0,
                    }

            self._models = sorted(
                [{"name": v["name"], "path": v["path"]} for v in merged.values()],
                key=lambda item: item["name"].lower(),
            )
            # 为可能存在的同名模型生成唯一的展示名（display_name）
            try:
                self._format_unique_display_names(self._models)
            except Exception:
                # 保持容错，若显示名生成失败则不影响原有字段
                pass
            self._model_error = "" if self._models else "未扫描到模型文件"
            self._model_scanned_at = utc_now_iso()

    def _format_unique_display_names(self, items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        给每个模型条目添加 `display_name` 字段以便前端展示。
        - 默认 `display_name` 为 `name`（文件名）
        - 若存在同名项，则逐级在前面追加父目录名（最近一级优先），直到在组内唯一或使用完整父路径
        """
        if not items:
            return items

        # 初始化默认 display_name
        for it in items:
            it["display_name"] = it.get("name", "")

        # 按小写 name 分组
        groups: Dict[str, List[int]] = {}
        for idx, it in enumerate(items):
            key = (it.get("name") or "").lower()
            groups.setdefault(key, []).append(idx)

        for key, idxs in groups.items():
            if len(idxs) <= 1:
                continue

            # 准备每个项的父路径零散片段
            parts_map: Dict[int, List[str]] = {}
            max_parts = 0
            for idx in idxs:
                try:
                    parent_parts = list(Path(items[idx]["path"]).parent.parts)
                except Exception:
                    parent_parts = []
                parts_map[idx] = parent_parts
                if len(parent_parts) > max_parts:
                    max_parts = len(parent_parts)

            resolved = False
            # 从最近一级父目录开始追加，逐级增加直到唯一
            for depth in range(1, max_parts + 1):
                candidate_map: Dict[str, List[int]] = {}
                for idx in idxs:
                    parts = parts_map.get(idx, [])
                    if depth <= len(parts):
                        used = parts[-depth:]
                    else:
                        used = parts
                    display = "/".join(used + [items[idx]["name"]]) if used else items[idx]["name"]
                    candidate_map.setdefault(display.lower(), []).append(idx)

                # 检查是否所有候选 display 唯一
                if all(len(v) == 1 for v in candidate_map.values()):
                    for disp, lst in candidate_map.items():
                        items[lst[0]]["display_name"] = disp
                    resolved = True
                    break

            if not resolved:
                # 退化为完整父路径 + name，保证唯一性
                for idx in idxs:
                    try:
                        full_parent = Path(items[idx]["path"]).parent.as_posix()
                    except Exception:
                        full_parent = ""
                    if full_parent:
                        items[idx]["display_name"] = f"{full_parent}/{items[idx]['name']}"
                    else:
                        items[idx]["display_name"] = items[idx]["name"]

        return items

    def refresh_once(self) -> None:
        cfg = load_runtime_config()
        self._scan_versions(cfg)
        self._scan_models(cfg)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

        def loop() -> None:
            while True:
                try:
                    self.refresh_once()
                except Exception:
                    pass

                cfg = load_runtime_config()
                interval = cfg.get("scan_interval_seconds", 30)
                if not isinstance(interval, int) or interval <= 0:
                    interval = 30
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True).start()

    def get_versions(self) -> Dict:
        with self._lock:
            return {
                "items": list(self._versions),
                "error": self._version_error,
                "scanned_at": self._version_scanned_at,
            }

    def get_models(self) -> Dict:
        with self._lock:
            return {
                "items": list(self._models),
                "error": self._model_error,
                "scanned_at": self._model_scanned_at,
            }


class LlamaParameterService:
    _SECTION_ALLOWLIST = {"Common params", "Sampling params", "Server-specific params"}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parameters: List[Dict] = self._default_parameters()
        self._source = "builtin"
        self._updated_at = ""
        self._error = ""
        self._started = False

    def _default_parameters(self) -> List[Dict]:
        return [
            {"name": "--model", "aliases": ["-m"], "description": "模型路径", "value_hint": "FNAME", "section": "Server-specific params"},
            {"name": "--host", "aliases": [], "description": "监听地址", "value_hint": "HOST", "section": "Server-specific params"},
            {"name": "--port", "aliases": [], "description": "监听端口", "value_hint": "PORT", "section": "Server-specific params"},
            {"name": "--ctx-size", "aliases": ["-c"], "description": "上下文长度", "value_hint": "N", "section": "Common params"},
            {"name": "--threads", "aliases": ["-t"], "description": "CPU 线程数", "value_hint": "N", "section": "Common params"},
            {"name": "--n-gpu-layers", "aliases": ["-ngl", "--gpu-layers"], "description": "GPU 层数", "value_hint": "N", "section": "Server-specific params"},
            {"name": "--temp", "aliases": ["--temperature"], "description": "采样温度", "value_hint": "N", "section": "Sampling params"},
            {"name": "--top-p", "aliases": [], "description": "Top-p 采样", "value_hint": "N", "section": "Sampling params"},
            {"name": "--top-k", "aliases": [], "description": "Top-k 采样", "value_hint": "N", "section": "Sampling params"},
            {"name": "--repeat-penalty", "aliases": [], "description": "重复惩罚", "value_hint": "N", "section": "Sampling params"},
            {"name": "--repeat-last-n", "aliases": [], "description": "重复惩罚窗口", "value_hint": "N", "section": "Sampling params"},
            {"name": "--draft-max", "aliases": ["--draft-n", "--spec-draft-n-max"], "description": "草稿最大 token 数", "value_hint": "N", "section": "Server-specific params"},
            {"name": "--draft-min", "aliases": ["--draft-n-min", "--spec-draft-n-min"], "description": "草稿最小 token 数", "value_hint": "N", "section": "Server-specific params"},
        ]

    def _cache_path(self) -> Path:
        cfg = load_runtime_config()
        cache_file = str(cfg.get("param_cache_file", ".cache/llama_params.json")).strip() or ".cache/llama_params.json"
        path = Path(cache_file)
        if not path.is_absolute():
            path = APP_ROOT / path
        return path

    def _readme_url(self) -> str:
        cfg = load_runtime_config()
        url = str(cfg.get("param_readme_url", "")).strip()
        return url or "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md"

    def _sync_enabled(self) -> bool:
        cfg = load_runtime_config()
        return bool(cfg.get("param_sync_enabled", False))

    def _sync_interval(self) -> int:
        cfg = load_runtime_config()
        interval = cfg.get("param_sync_interval_seconds", 86400)
        return interval if isinstance(interval, int) and interval > 0 else 86400

    @staticmethod
    def _clean_description(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()

    @staticmethod
    def _extract_flags(raw_flags: str) -> List[str]:
        flags: List[str] = []
        for chunk in (raw_flags or "").split(","):
            match = re.search(r"(--?[A-Za-z0-9][A-Za-z0-9-]*)", chunk.strip())
            if match:
                flag = match.group(1).strip()
                if flag not in flags:
                    flags.append(flag)
        return flags

    @staticmethod
    def _extract_value_hint(raw_flags: str, primary_flag: str) -> str:
        for chunk in (raw_flags or "").split(","):
            chunk = chunk.strip()
            if primary_flag not in chunk:
                continue
            hint = chunk.split(primary_flag, 1)[1].strip()
            hint = hint.lstrip(",").strip()
            if hint.startswith("<") and hint.endswith(">"):
                hint = hint[1:-1].strip()
            return hint
        return ""

    def _parse_readme(self, text: str) -> List[Dict]:
        items: List[Dict] = []
        seen = set()
        current_section = ""
        in_target_section = False

        for line in text.splitlines():
            heading = re.match(r"^###\s+(.+)$", line.strip())
            if heading:
                current_section = heading.group(1).strip()
                in_target_section = current_section in self._SECTION_ALLOWLIST
                continue

            if not in_target_section:
                continue

            stripped = line.strip()
            if not stripped.startswith("|"):
                continue

            columns = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(columns) < 2:
                continue

            raw_flags = columns[0]
            description = self._clean_description(columns[1])
            if not raw_flags or not description:
                continue
            if re.fullmatch(r"[-\s]+", raw_flags):
                continue

            flags = self._extract_flags(raw_flags)
            if not flags:
                continue

            primary_flag = next((flag for flag in flags if flag.startswith("--")), flags[0])
            if primary_flag in seen:
                continue

            seen.add(primary_flag)
            items.append(
                {
                    "name": primary_flag,
                    "aliases": flags,
                    "description": description,
                    "value_hint": self._extract_value_hint(raw_flags, primary_flag),
                    "section": current_section,
                }
            )

        return items

    def _load_cache(self) -> List[Dict]:
        cache_path = self._cache_path()
        if not cache_path.exists():
            return []

        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return []

            normalized: List[Dict] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
                normalized.append(
                    {
                        "name": name,
                        "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
                        "description": self._clean_description(str(item.get("description", ""))),
                        "value_hint": self._clean_description(str(item.get("value_hint", ""))),
                        "section": str(item.get("section", "")).strip(),
                    }
                )
            return normalized
        except Exception:
            return []

    def _save_cache(self, items: List[Dict], source: str) -> None:
        cache_path = self._cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "source": source,
                        "updated_at": utc_now_iso(),
                        "items": items,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _download_readme(self) -> str:
        req = Request(self._readme_url(), headers={"User-Agent": "llama-manager/1.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def refresh_from_cache(self) -> Dict:
        cached = self._load_cache()
        with self._lock:
            if cached:
                self._parameters = cached
                self._source = "cache"
                self._error = ""
                self._updated_at = utc_now_iso()
            elif not self._parameters:
                self._parameters = self._default_parameters()
                self._source = "builtin"
        return self.get_parameters()

    def sync_once(self) -> Dict:
        try:
            readme = self._download_readme()
            items = self._parse_readme(readme)
            if not items:
                raise ValueError("未能从 README 中解析到参数表")
            with self._lock:
                self._parameters = items
                self._source = "remote"
                self._error = ""
                self._updated_at = utc_now_iso()
            self._save_cache(items, "remote")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            with self._lock:
                self._error = str(exc)
                if not self._parameters:
                    self._parameters = self._default_parameters()
                    self._source = "builtin"
                    self._updated_at = utc_now_iso()
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                if not self._parameters:
                    self._parameters = self._default_parameters()
                    self._source = "builtin"
                    self._updated_at = utc_now_iso()
        return self.get_parameters()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh_from_cache()

        if not self._sync_enabled():
            return

        def loop() -> None:
            while True:
                try:
                    self.sync_once()
                except Exception:
                    pass
                time.sleep(self._sync_interval())

        threading.Thread(target=loop, daemon=True).start()

    def get_parameters(self) -> Dict:
        with self._lock:
            return {
                "items": list(self._parameters),
                "source": self._source,
                "updated_at": self._updated_at,
                "error": self._error,
                "count": len(self._parameters),
                "sync_enabled": self._sync_enabled(),
            }


app = Flask(__name__)
manager = InstanceManager()
auto_scan_service = AutoScanService(manager)
auto_scan_service.start()
param_service = LlamaParameterService()
param_service.start()


DAEMON_PROCESS_NAME = "daemon.py"


def _ensure_daemon_running():
    if _is_daemon_running():
        return

    _start_daemon_process()
    if _wait_for_daemon_ready(timeout=10):
        return


def _acquire_file_lock(lock_fd):
    """Cross-platform file locking"""
    if sys.platform == "win32":
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


def _release_file_lock(lock_fd):
    """Release cross-platform file lock"""
    if sys.platform == "win32":
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass


# Module-level flag for first request initialization
_daemon_initialized = False


def _ensure_daemon_ready():
    """Initialize daemon on first request (Flask 2.0+ compatible)"""
    global _daemon_initialized
    if _daemon_initialized:
        return
    
    _daemon_initialized = True
    lock_path = APP_ROOT / "daemon.lock"
    lock_fd = None
    try:
        # 以阻塞式文件锁保护：只有获得锁的进程会尝试启动 daemon
        lock_fd = open(lock_path, "w")
        if not _acquire_file_lock(lock_fd):
            # 其他进程正在初始化 daemon，直接返回
            lock_fd.close()
            return

        # 双重检查，防止竞争
        if _is_daemon_running():
            try:
                _release_file_lock(lock_fd)
                lock_fd.close()
            except Exception:
                pass
            return

        _start_daemon_process()
        # 延长等待时间以适应慢启动场景
        _wait_for_daemon_ready(timeout=30)
    except Exception as e:
        print(f"Failed to ensure daemon running: {e}")
    finally:
        if lock_fd:
            try:
                _release_file_lock(lock_fd)
                lock_fd.close()
            except Exception:
                pass


# Flask 2.0+ compatible initialization
if hasattr(app, 'before_serving'):
    # Flask 2.1+
    @app.before_serving
    def init_daemon():
        _ensure_daemon_ready()
else:
    # Flask 2.0 fallback
    @app.before_request
    def init_daemon_before_request():
        _ensure_daemon_ready()


def _sse_event(event: str, payload: Dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/daemon/status")
def daemon_status():
    running = _is_daemon_running()
    info = _get_daemon_info() if running else None
    return jsonify({"running": running, "pid": info[0] if info else None})


@app.get("/api/daemon/status/stream")
def daemon_status_stream():
    def generate():
        last_status = None
        last_instance_status = None
        while True:
            try:
                running = _is_daemon_running()
                info = _get_daemon_info() if running else None
                pid = info[0] if info else None

                status = {"running": running, "pid": pid}
                if status != last_status:
                    last_status = status
                    yield _sse_event("status", status)

                if running:
                    items = manager.list_instances()
                    instance_status = {item["instance_id"]: item.get("status") for item in items}
                    if instance_status != last_instance_status:
                        last_instance_status = instance_status
                        yield _sse_event("instances", {"items": items})

            except Exception:
                pass
            time.sleep(2)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)


@app.post("/api/daemon/start")
def daemon_start():
    if _is_daemon_running():
        return jsonify({"error": "守护进程已在运行"}), 400

    _start_daemon_process()
    if not _wait_for_daemon_ready(timeout=10):
        return jsonify({"error": "守护进程启动失败"}), 500
    return jsonify({"success": True})


@app.post("/api/daemon/stop")
def daemon_stop():
    if stop_daemon_via_tcp():
        return jsonify({"success": True})
    if not _is_daemon_running():
        return jsonify({"error": "守护进程未运行"}), 400
    return jsonify({"error": "守护进程停止超时"}), 504


@app.get("/api/instances")
def list_instances():
    response = _control_request({"action": "list", "target": "instance"})
    if response and response.get("success"):
        return jsonify({"items": response.get("instances", [])})
    return jsonify({"items": manager.list_instances()})


@app.post("/api/instances")
def create_instance():
    body = request.get_json(silent=True) or {}
    try:
        created = manager.create_instance(
            name=body.get("name", ""),
            server_dir=body.get("server_dir", body.get("executable_path", "")),
            visual_args=body.get("visual_args", {}),
            freeform_args=body.get("freeform_args", ""),
            env_vars=body.get("env_vars", []),
        )
        return jsonify(created), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/instances/<instance_id>/stop")
def stop_instance(instance_id: str):
    response = _control_request({"action": "stop", "target": "instance", "instance_id": instance_id})
    if response and response.get("success"):
        return jsonify(response.get("instance", {}))
    return jsonify({"error": response.get("error", "failed") if response else "daemon not running"}), 400


@app.post("/api/instances/<instance_id>/start")
def start_instance(instance_id: str):
    response = _control_request({"action": "start", "target": "instance", "instance_id": instance_id})
    if response and response.get("success"):
        return jsonify(response.get("instance", {}))
    return jsonify({"error": response.get("error", "failed") if response else "daemon not running"}), 400


@app.put("/api/instances/<instance_id>")
def update_instance(instance_id: str):
    body = request.get_json(silent=True) or {}
    try:
        updated = manager.update_instance(
            instance_id=instance_id,
            name=body.get("name", ""),
            server_dir=body.get("server_dir", body.get("executable_path", "")),
            visual_args=body.get("visual_args", {}),
            freeform_args=body.get("freeform_args", ""),
            env_vars=body.get("env_vars", []),
        )
        # 尝试通过 daemon 的 TCP 控制接口重启实例：先 stop 再 start
        try:
            # 停止旧进程（若在运行）
            _control_request({"action": "stop", "target": "instance", "instance_id": instance_id})
            # 启动实例（重启）
            start_resp = _control_request({"action": "start", "target": "instance", "instance_id": instance_id})
            if start_resp and start_resp.get("success"):
                return jsonify(start_resp.get("instance", updated))
        except Exception:
            # 忽略控制请求错误，仍返回已保存的实例信息
            pass

        return jsonify(updated)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/instances/<instance_id>", methods=["DELETE"])
def delete_instance(instance_id: str):
    try:
        result = manager.delete_instance(instance_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/instances/<instance_id>/logs")
def get_logs(instance_id: str):
    lines = request.args.get("lines", type=int, default=200)
    record = manager.get_instance(instance_id)
    if record:
        return jsonify(
            {
                "instance_id": instance_id,
                "status": record.status,
                "lines": record.get_logs(lines),
            }
        )

    matched = None
    for item in manager._list_persisted_instances():
        if item["instance_id"] == instance_id:
            matched = item
            break

    if not matched:
        return jsonify({"error": "实例不存在"}), 404

    return jsonify(
        {
            "instance_id": instance_id,
            "status": matched["status"],
            "lines": manager._read_log_file_tail(matched["log_file"], lines),
        }
    )


@app.get("/api/instances/<instance_id>/logs/before")
def get_logs_before(instance_id: str):
    offset = request.args.get("offset", type=int, default=0)
    limit = request.args.get("limit", type=int, default=200)
    limit = min(max(limit, 1), 500)

    matched = None
    for item in manager._list_persisted_instances():
        if item["instance_id"] == instance_id:
            matched = item
            break

    if not matched:
        return jsonify({"error": "实例不存在"}), 404

    lines = manager._read_log_file_from(matched["log_file"], offset, limit)
    return jsonify(
        {
            "instance_id": instance_id,
            "lines": lines,
            "has_more": len(lines) == limit,
        }
    )


@app.get("/api/instances/<instance_id>/logs/stream")
def stream_logs(instance_id: str):
    lines = request.args.get("lines", type=int, default=300)
    if lines <= 0:
        lines = 300
    lines = min(lines, 2000)

    def generate():
        snapshot_sent = False
        last_pos = 0
        last_ping_at = time.monotonic()
        persisted = manager._get_persisted_instance(instance_id)

        if not persisted:
            yield _sse_event("log-error", {"error": "实例不存在"})
            yield _sse_event("end", {"reason": "not-found"})
            return

        log_file = Path(persisted["log_file"])
        if not log_file.exists():
            yield _sse_event("snapshot", {"instance_id": instance_id, "status": persisted["status"], "lines": []})
            yield _sse_event("end", {"reason": "no-log-file"})
            return

        while True:
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(0, 2)
                    file_size = f.tell()

                    if not snapshot_sent:
                        f.seek(0)
                        all_lines = f.readlines()
                        tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                        start_offset = max(0, len(all_lines) - len(tail_lines))
                        last_pos = file_size
                        snapshot_sent = True
                        yield _sse_event(
                            "snapshot",
                            {
                                "instance_id": instance_id,
                                "status": persisted["status"],
                                "lines": [line.rstrip("\n\r") for line in tail_lines],
                                "start_offset": start_offset,
                            },
                        )
                    else:
                        if file_size > last_pos:
                            f.seek(last_pos)
                            new_lines = f.readlines()
                            last_pos = file_size
                            for line in new_lines:
                                line = line.rstrip("\n\r")
                                if line:
                                    yield _sse_event(
                                        "append",
                                        {
                                            "instance_id": instance_id,
                                            "status": persisted["status"],
                                            "line": line,
                                        },
                                    )

                    now = time.monotonic()
                    if now - last_ping_at >= 15:
                        last_ping_at = now
                        yield ": ping\n\n"

                    time.sleep(0.5)

            except GeneratorExit:
                break
            except Exception:
                time.sleep(0.5)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)


@app.post("/api/command-preview")
def command_preview():
    body = request.get_json(silent=True) or {}
    try:
        preview = manager._build_command(
            server_dir=body.get("server_dir", body.get("executable_path", "")),
            visual_args=body.get("visual_args", {}),
            freeform_args=body.get("freeform_args", ""),
        )
        return jsonify({"command": preview})
    except Exception as e:
        server_dir = body.get("server_dir", body.get("executable_path", ""))
        if server_dir:
            preview = _build_partial_command(server_dir, body.get("visual_args", {}), body.get("freeform_args", ""))
            if preview:
                return jsonify({"command": preview})
        return jsonify({"error": str(e)}), 400


def _build_partial_command(server_dir: str, visual_args: Dict, freeform_args: str) -> List[str]:
    try:
        from pathlib import Path
        raw = (server_dir or "").strip()
        if not raw:
            return []
        path = Path(raw)
        exe = str(path) if path.is_file() else None
        if not exe:
            exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
            candidate = path / exe_name
            if candidate.exists():
                exe = str(candidate)
        if not exe:
            return []
        cmd = [exe]
        model = (visual_args.get("model_path") or "").strip()
        draft_model = (visual_args.get("draft_model_path") or "").strip()
        host = (visual_args.get("host") or "").strip()
        port = visual_args.get("port")
        n_ctx = visual_args.get("n_ctx")
        n_threads = visual_args.get("n_threads")
        gpu_layers = visual_args.get("gpu_layers")
        draft_max = visual_args.get("draft_max")
        draft_min = visual_args.get("draft_min")
        extra_kv = visual_args.get("extra_flags") or []
        if model:
            cmd.extend(["--model", model])
        if draft_model:
            cmd.extend(["--model-draft", draft_model])
        if host:
            cmd.extend(["--host", host])
        if port:
            cmd.extend(["--port", str(port)])
        if n_ctx:
            cmd.extend(["--ctx-size", str(n_ctx)])
        if n_threads:
            cmd.extend(["--threads", str(n_threads)])
        if gpu_layers is not None and str(gpu_layers).strip() != "":
            cmd.extend(["--n-gpu-layers", str(gpu_layers)])
        if draft_max is not None:
            cmd.extend(["--draft-max", str(draft_max)])
        if draft_min is not None:
            cmd.extend(["--draft-min", str(draft_min)])
        for item in extra_kv:
            enabled_raw = item.get("enabled", True)
            enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).strip().lower() not in {"0", "false", "off", "no"}
            if not enabled:
                continue
            key = (item.get("key") or "").strip()
            value = (item.get("value") or "").strip()
            if not key:
                continue
            if not key.startswith("-"):
                key = f"--{key}"
            cmd.append(key)
            if value:
                cmd.append(value)
        if freeform_args.strip():
            try:
                cmd.extend(shlex.split(freeform_args, posix=(os.name != "nt")))
            except Exception:
                pass
        return cmd
    except Exception:
        return []


@app.get("/api/llama/discover")
def discover_llama():
    return jsonify(auto_scan_service.get_versions())


@app.get("/api/models/discover")
def discover_models():
    return jsonify(auto_scan_service.get_models())


@app.get("/api/llama/parameters")
def get_llama_parameters():
    return jsonify(param_service.get_parameters())


@app.post("/api/llama/parameters/refresh")
def refresh_llama_parameters():
    return jsonify(param_service.sync_once())


if __name__ == "__main__":
    host = os.environ.get("LLAMA_MANAGER_HOST", "0.0.0.0")
    port = int(os.environ.get("LLAMA_MANAGER_PORT", "8787"))
    app.run(host=host, port=port, debug=False)
