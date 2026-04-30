import datetime as dt
import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import psutil


APP_ROOT = Path(__file__).parent
DAEMON_PID_FILE = APP_ROOT / "daemon.pid"


def _get_daemon_info() -> Optional[tuple]:
    """读取 daemon PID 和端口"""
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        with open(DAEMON_PID_FILE, "r") as f:
            lines = f.read().strip().split("\n")
        if len(lines) < 2:
            return None
        return int(lines[0]), int(lines[1])
    except:
        return None


def _is_daemon_running() -> bool:
    """检查 daemon 是否运行"""
    info = _get_daemon_info()
    if not info:
        return False
    try:
        proc = psutil.Process(info[0])
        return proc.is_running()
    except psutil.NoSuchProcess:
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

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        response = sock.recv(8192)
        sock.close()
        return json.loads(response.decode("utf-8"))
    except Exception as e:
        print(f"TCP request failed: {e}")
        return None


def stop_daemon_via_tcp() -> bool:
    """通过 TCP 连接停止 daemon"""
    response = _control_request({"action": "stop", "target": "daemon"}, timeout=10)
    if response and response.get("success"):
        return True
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
        with self._db_lock:
            with self._db_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM instances ORDER BY datetime(updated_at) DESC"
                ).fetchall()

        items: List[Dict] = []
        for row in rows:
            items.append(
                {
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
            self._model_error = "" if self._models else "未扫描到模型文件"
            self._model_scanned_at = utc_now_iso()

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


app = Flask(__name__)
manager = InstanceManager()
auto_scan_service = AutoScanService(manager)
auto_scan_service.start()


DAEMON_PROCESS_NAME = "daemon.py"


def _ensure_daemon_running():
    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("daemon.py" in str(arg) for arg in cmdline):
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    print("[App] Daemon not running, starting...")
    daemon_path = APP_ROOT / "daemon.py"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["python", str(daemon_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=True,
    )
    time.sleep(2)


_ensure_daemon_running()


def _sse_event(event: str, payload: Dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/daemon/status")
def daemon_status():
    import psutil

    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("daemon.py" in str(arg) for arg in cmdline):
                return jsonify({"running": True, "pid": proc.info["pid"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return jsonify({"running": False, "pid": None})


@app.get("/api/daemon/status/stream")
def daemon_status_stream():
    def generate():
        last_status = None
        last_instance_status = None
        while True:
            try:
                import psutil

                current_pid = os.getpid()
                running = False
                pid = None
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        if proc.info["pid"] == current_pid:
                            continue
                        cmdline = proc.info.get("cmdline") or []
                        if any("daemon.py" in str(arg) for arg in cmdline):
                            running = True
                            pid = proc.info["pid"]
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

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
    import psutil

    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any("daemon.py" in str(arg) for arg in cmdline):
                return jsonify({"error": "守护进程已在运行"}), 400
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    daemon_path = APP_ROOT / "daemon.py"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["python", str(daemon_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=True,
    )
    time.sleep(2)
    return jsonify({"success": True})


@app.post("/api/daemon/stop")
def daemon_stop():
    if stop_daemon_via_tcp():
        return jsonify({"success": True})
    return jsonify({"error": "守护进程未运行"}), 400


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


if __name__ == "__main__":
    host = os.environ.get("LLAMA_MANAGER_HOST", "0.0.0.0")
    port = int(os.environ.get("LLAMA_MANAGER_PORT", "8787"))
    app.run(host=host, port=port, debug=False)
