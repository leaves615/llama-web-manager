#!/usr/bin/env python3
"""
Llama Server Manager - 守护进程
独立管理 llama-server 进程，重启主应用不影响运行中的实例
"""
import datetime as dt
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import yaml


APP_ROOT = Path(__file__).parent
LOG_DIR = APP_ROOT / "logs"
DB_FILE = APP_ROOT / "instances.db"
CONFIG_FILE = APP_ROOT / "config.yaml"
DAEMON_LOG_FILE = LOG_DIR / "daemon.log"
DAEMON_PID_FILE = APP_ROOT / "daemon.pid"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def find_free_port():
    """获取随机可用端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        return s.getsockname()[1]


def write_daemon_pid(port: int):
    """写入 daemon PID 和端口信息"""
    with open(DAEMON_PID_FILE, "w") as f:
        f.write(f"{os.getpid()}\n{port}\n")


def read_daemon_info():
    """读取 daemon PID 和端口信息"""
    if not DAEMON_PID_FILE.exists():
        return None, None
    with open(DAEMON_PID_FILE, "r") as f:
        lines = f.read().strip().split("\n")
    if len(lines) >= 2:
        return int(lines[0]), int(lines[1])
    return None, None


def setup_logging():
    """配置 daemon 日志输出到文件和终端"""
    logger = logging.getLogger("daemon")
    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(DAEMON_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S"
    ))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


daemon_logger = setup_logging()


def kill_process_tree(pid: int, include_parent: bool = True) -> bool:
    """跨平台终止进程树"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return True

    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass

    gone, alive = psutil.wait_procs(children, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass

    if include_parent:
        try:
            parent.terminate()
            parent.wait(timeout=3)
        except psutil.TimeoutExpired:
            try:
                parent.kill()
                parent.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass
        except psutil.NoSuchProcess:
            pass

    return parent.is_running() == False


def now_iso():
    """返回本地时间的 ISO 格式字符串（毫秒精度，无时区信息）"""
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def load_config() -> Dict:
    default_cfg = {
        "scan_roots": [],
        "scan_max_depth": 3,
        "scan_interval_seconds": 60,
        "model_scan_roots": [],
        "model_scan_max_depth": 3,
        "model_extensions": [".gguf", ".bin"],
    }
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                return {**default_cfg, **cfg}
    except Exception:
        pass
    return default_cfg


class LlamaInstance:
    def __init__(
        self,
        instance_id: str,
        name: str,
        executable_path: str,
        command: List[str],
        log_file: Path,
        visual_args: Dict,
        freeform_args: str,
        env_vars: List[Dict],
        created_at: str,
    ):
        self.instance_id = instance_id
        self.name = name
        self.executable_path = executable_path
        self.command = command
        self.log_file = log_file
        self.visual_args = visual_args
        self.freeform_args = freeform_args
        self.env_vars = env_vars
        self.created_at = created_at
        self.process: Optional[subprocess.Popen] = None
        self._stopped_by_manager = False
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def pid(self) -> int:
        return self.process.pid if self.process else 0

    @property
    def status(self) -> str:
        if self._stopped_by_manager:
            return "stopped"
        if self.process:
            return "running" if self.process.poll() is None else f"exited({self.process.returncode})"
        return "stopped"

    def start(self):
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        env = os.environ.copy()
        for item in self.env_vars:
            key = (item.get("key") or "").strip()
            value = (item.get("value") or "").strip()
            if key:
                env[key] = value

        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )

        self._start_log_capture()
        daemon_logger.info(f"Started instance {self.name} (PID: {self.pid})")

    def stop(self):
        self._stopped_by_manager = True
        pid = self.pid
        daemon_logger.info(f"Stopping instance {self.name} (PID: {pid})...")
        if pid == 0:
            daemon_logger.warning(f"Instance {self.name} has no PID")
            return

        success = kill_process_tree(pid, include_parent=True)
        if not success:
            try:
                parent = psutil.Process(pid)
                if parent.is_running():
                    daemon_logger.warning(f"Process still alive, trying SIGKILL...")
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False)
                    else:
                        os.kill(pid, signal.SIGKILL)
            except psutil.NoSuchProcess:
                pass

        try:
            parent_check = psutil.Process(pid)
            if parent_check.is_running():
                daemon_logger.warning(f"Instance {self.name} (PID: {pid}) may still be running")
            else:
                daemon_logger.info(f"Instance {self.name} (PID: {pid}) stopped successfully")
        except psutil.NoSuchProcess:
            daemon_logger.info(f"Instance {self.name} (PID: {pid}) stopped successfully")
        daemon_logger.info(f"Stopped instance {self.name}")

    def _start_log_capture(self):
        def capture():
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"\n[{now_iso()}] === Daemon attached, logging started ===\n")
                f.write(f"[{now_iso()}] command: {' '.join(self.command)}\n")
                f.flush()

                stdout = self.process.stdout
                if stdout is None:
                    return

                for line in stdout:
                    clean = line.rstrip("\n")
                    msg = f"[{now_iso()}] {clean}"
                    f.write(msg + "\n")
                    f.flush()

                return_code = self.process.poll()
                if return_code is None:
                    try:
                        return_code = self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        return_code = -1
                        self.process.kill()

                ended = f"[{now_iso()}] process exited with code {return_code}"
                f.write(ended + "\n")
                f.flush()

        thread = threading.Thread(target=capture, daemon=True)
        thread.start()
        self._reader_thread = thread


class DaemonManager:
    def __init__(self):
        self.instances: Dict[str, LlamaInstance] = {}
        self._running = True
        self._lock = threading.Lock()
        self._db_conn = sqlite3.connect(DB_FILE, timeout=5, check_same_thread=False)
        self._db_conn.row_factory = sqlite3.Row
        self._control_server = None
        self._control_thread = None
        self._control_port = None

    def close(self):
        self._running = False
        self._stop_control_server()
        self._db_conn.close()

    def _start_control_server(self):
        """启动控制服务器"""
        self._control_port = find_free_port()
        write_daemon_pid(self._control_port)
        daemon_logger.info(f"Control server starting on port {self._control_port}")

        def server_thread():
            self._control_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._control_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._control_server.bind(("127.0.0.1", self._control_port))
            self._control_server.listen(8)
            self._control_server.settimeout(1.0)

            while self._running:
                try:
                    conn, addr = self._control_server.accept()
                    conn.settimeout(5)
                    try:
                        data = conn.recv(4096).decode("utf-8").strip()
                        if not data:
                            conn.sendall(json.dumps({"success": False, "error": "empty request"}).encode())
                            conn.close()
                            continue

                        daemon_logger.debug(f"Received control command: {data[:100]}")
                        request = json.loads(data)
                        action = request.get("action")
                        target = request.get("target")

                        if target is None:
                            conn.sendall(json.dumps({"success": False, "error": "target is required"}).encode())
                            conn.close()
                            continue

                        if action == "stop" and target == "daemon":
                            conn.sendall(json.dumps({"success": True}).encode())
                            conn.close()
                            daemon_logger.info("Control server received stop command")
                            self._running = False
                            return

                        if action == "list" and target == "daemon":
                            conn.sendall(json.dumps({
                                "success": True,
                                "status": "running",
                                "instances": len(self.instances)
                            }).encode())
                            conn.close()
                            continue

                        response = self._handle_command(request)
                        conn.sendall(json.dumps(response, ensure_ascii=False).encode())
                        conn.close()
                    except json.JSONDecodeError as e:
                        conn.sendall(json.dumps({"success": False, "error": f"invalid JSON: {e}"}).encode())
                        conn.close()
                    except Exception as e:
                        daemon_logger.error(f"Control server error: {e}")
                        try:
                            conn.sendall(json.dumps({"success": False, "error": str(e)}).encode())
                        except:
                            pass
                        conn.close()
                except socket.timeout:
                    continue
                except Exception as e:
                    daemon_logger.error(f"Control server accept error: {e}")
                    break

            try:
                self._control_server.close()
            except:
                pass

        self._control_thread = threading.Thread(target=server_thread, daemon=True)
        self._control_thread.start()

    def _handle_command(self, request: Dict) -> Dict:
        """处理控制命令"""
        action = request.get("action")
        instance_id = request.get("instance_id")
        instance_ids = request.get("instance_ids", [])

        try:
            if action == "start":
                return self._do_start_instance(instance_id)
            elif action == "stop":
                return self._do_stop_instance(instance_id)
            elif action == "batch_start":
                return self._do_batch_start(instance_ids)
            elif action == "batch_stop":
                return self._do_batch_stop(instance_ids)
            elif action == "list":
                return self._do_list_instances()
            elif action == "get":
                return self._do_get_instance(instance_id)
            else:
                return {"success": False, "error": f"unknown action: {action}"}
        except Exception as e:
            daemon_logger.error(f"Command {action} error: {e}")
            return {"success": False, "error": str(e)}

    def _stop_control_server(self):
        """停止控制服务器"""
        try:
            if self._control_server:
                self._control_server.close()
        except:
            pass
        if DAEMON_PID_FILE.exists():
            DAEMON_PID_FILE.unlink()

    def _get_persisted_instance(self, instance_id: str) -> Optional[Dict]:
        cursor = self._db_execute("SELECT * FROM instances WHERE instance_id = ?", (instance_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def _serialize_instance(self, instance_id: str, instance: Optional["LlamaInstance"] = None) -> Dict:
        row = self._get_persisted_instance(instance_id)
        if not row:
            return {"instance_id": instance_id, "error": "not found"}

        result = {
            "instance_id": row["instance_id"],
            "name": row["name"],
            "executable_path": row["executable_path"],
            "status": row["status"],
            "pid": row["pid"],
            "created_at": row["created_at"],
            "command": json.loads(row["command_json"]) if row["command_json"] else [],
            "visual_args": json.loads(row["visual_args_json"]) if row["visual_args_json"] else {},
            "freeform_args": row["freeform_args"],
            "log_file": str(row["log_file"]),
        }

        if instance:
            result["status"] = instance.status

        return result

    def _do_start_instance(self, instance_id: str) -> Dict:
        """启动单个实例"""
        if not instance_id:
            return {"success": False, "error": "instance_id required"}

        row = self._get_persisted_instance(instance_id)
        if not row:
            return {"success": False, "error": "instance not found"}

        if instance_id in self.instances:
            inst = self.instances[instance_id]
            if inst.status == "running":
                return {"success": True, "instance": self._serialize_instance(instance_id, inst)}

        instance = self._start_instance_from_row(row)
        return {"success": True, "instance": self._serialize_instance(instance_id, instance)}

    def _do_stop_instance(self, instance_id: str) -> Dict:
        """停止单个实例"""
        if not instance_id:
            return {"success": False, "error": "instance_id required"}

        instance = self.instances.get(instance_id)
        if instance:
            instance.stop()
            del self.instances[instance_id]

        self._db_execute(
            "UPDATE instances SET status = 'stopped', pid = NULL WHERE instance_id = ?",
            (instance_id,)
        )

        return {"success": True, "instance": self._serialize_instance(instance_id)}

    def _do_batch_start(self, instance_ids: List[str]) -> Dict:
        """批量启动"""
        results = []
        for instance_id in instance_ids:
            results.append(self._do_start_instance(instance_id))
        return {"success": True, "results": results}

    def _do_batch_stop(self, instance_ids: List[str]) -> Dict:
        """批量停止"""
        results = []
        for instance_id in instance_ids:
            results.append(self._do_stop_instance(instance_id))
        return {"success": True, "results": results}

    def _do_list_instances(self) -> Dict:
        """列出所有实例"""
        cursor = self._db_execute("SELECT instance_id FROM instances")
        rows = cursor.fetchall()
        instances = []
        for row in rows:
            instance_id = row["instance_id"]
            instance = self.instances.get(instance_id)
            instances.append(self._serialize_instance(instance_id, instance))
        return {"success": True, "instances": instances}

    def _do_get_instance(self, instance_id: str) -> Dict:
        """获取单个实例"""
        if not instance_id:
            return {"success": False, "error": "instance_id required"}
        instance = self.instances.get(instance_id)
        return {"success": True, "instance": self._serialize_instance(instance_id, instance)}

    def _db_execute(self, sql: str, params=()):
        cursor = self._db_conn.cursor()
        cursor.execute(sql, params)
        self._db_conn.commit()
        return cursor

    def load_config(self):
        return load_config()

    def scan_existing(self):
        cursor = self._db_execute("SELECT * FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            if not pid:
                continue
            try:
                process = psutil.Process(pid)
                if not process.is_running():
                    continue

                instance_id = row["instance_id"]
                self._db_execute(
                    "UPDATE instances SET status = 'stopped', pid = NULL WHERE instance_id = ?",
                    (instance_id,),
                )
                daemon_logger.info(f"Marked stale instance {instance_id} as stopped")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._db_execute(
                    "UPDATE instances SET status = 'stopped', pid = NULL WHERE pid = ?",
                    (pid,),
                )

    def load_instances(self):
        cursor = self._db_execute("SELECT * FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            if not pid:
                continue
            try:
                process = psutil.Process(pid)
                if not process.is_running():
                    continue

                instance_id = row["instance_id"]
                log_file = Path(row["log_file"])

                instance = LlamaInstance(
                    instance_id=instance_id,
                    name=row["name"],
                    executable_path=row["executable_path"],
                    command=json.loads(row["command_json"]),
                    log_file=log_file,
                    visual_args=json.loads(row["visual_args_json"]),
                    freeform_args=row["freeform_args"],
                    env_vars=json.loads(row.get("env_vars_json") or "[]"),
                    created_at=row["created_at"],
                )
                instance._stopped_by_manager = True
                self.instances[instance_id] = instance
                daemon_logger.info(f"Restored instance {instance.name} (PID: {pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                daemon_logger.error(f"Failed to restore {row['instance_id']}: {e}")

    def process_commands(self):
        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'start'")
        rows = cursor.fetchall()
        if rows:
            daemon_logger.info(f"Processing {len(rows)} start command(s)")
        for row in rows:
            self._start_instance_from_row(row)

        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'stop'")
        rows = cursor.fetchall()
        if rows:
            daemon_logger.info(f"Processing {len(rows)} stop command(s)")
        for row in rows:
            self._stop_instance(row)

    def _start_instance_from_row(self, row: Dict) -> Optional["LlamaInstance"]:
        """从数据库行启动实例，返回启动的实例或None"""
        instance_id = row["instance_id"]
        if instance_id in self.instances:
            instance = self.instances[instance_id]
            if instance.status == "running":
                daemon_logger.info(f"Instance {instance.name} already running, stopping and restarting...")
                instance.stop()
                del self.instances[instance_id]
            else:
                return None

        try:
            command = json.loads(row["command_json"])
            log_file = Path(row["log_file"])

            instance = LlamaInstance(
                instance_id=instance_id,
                name=row["name"],
                executable_path=row["executable_path"],
                command=command,
                log_file=log_file,
                visual_args=json.loads(row["visual_args_json"]),
                freeform_args=row["freeform_args"],
                env_vars=json.loads(row.get("env_vars_json") or "[]"),
                created_at=row["created_at"],
            )
            instance.start()

            self.instances[instance_id] = instance

            self._db_execute(
                "UPDATE instances SET pid = ?, status = 'running', command = NULL, updated_at = ? WHERE instance_id = ?",
                (instance.pid, now_iso(), instance_id),
            )
            daemon_logger.info(f"Started {instance.name}")
            return instance
        except Exception as e:
            daemon_logger.error(f"Failed to start {instance_id}: {e}")
            self._db_execute(
                "UPDATE instances SET status = 'error', command = NULL, updated_at = ? WHERE instance_id = ?",
                (now_iso(), instance_id),
            )
            return None

    def process_commands(self):
        """处理数据库命令（兼容旧方案）"""
        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'start'")
        rows = cursor.fetchall()
        if rows:
            daemon_logger.info(f"Processing {len(rows)} start command(s)")
        for row in rows:
            self._start_instance_from_row(dict(row))

        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'stop'")
        rows = cursor.fetchall()
        if rows:
            daemon_logger.info(f"Processing {len(rows)} stop command(s)")
        for row in rows:
            instance_id = dict(row)["instance_id"]
            instance = self.instances.get(instance_id)
            if instance:
                instance.stop()
                del self.instances[instance_id]
            self._db_execute(
                "UPDATE instances SET pid = NULL, status = 'stopped', command = NULL, updated_at = ? WHERE instance_id = ?",
                (now_iso(), instance_id),
            )

    def check_status(self):
        to_remove = []
        for instance_id, instance in self.instances.items():
            if instance.status == "stopped":
                self._db_execute(
                    "UPDATE instances SET status = 'stopped', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (now_iso(), instance_id),
                )
                to_remove.append(instance_id)

        for instance_id in to_remove:
            del self.instances[instance_id]

        cursor = self._db_execute("SELECT instance_id, pid FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            if not pid:
                continue
            instance_id = row["instance_id"]
            try:
                proc = psutil.Process(pid)
                status = proc.status()
                if status not in (psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING):
                    self._db_execute(
                        "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                        (now_iso(), instance_id),
                    )
                    daemon_logger.warning(f"Instance {instance_id} process exited (status: {status})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._db_execute(
                    "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (now_iso(), instance_id),
                )
                daemon_logger.warning(f"Instance {instance_id} process not found")

    def run(self):
        def do_shutdown():
            daemon_logger.info("Received shutdown signal")
            self._running = False

        if os.name != "nt":
            signal.signal(signal.SIGTERM, lambda s, f: do_shutdown())
            signal.signal(signal.SIGINT, lambda s, f: do_shutdown())

        self._start_control_server()
        daemon_logger.info("Starting daemon...")
        self.scan_existing()
        self.load_instances()

        daemon_logger.info("Daemon running")

        while self._running:
            try:
                self.process_commands()
                self.check_status()
            except KeyboardInterrupt:
                daemon_logger.info("Received interrupt signal")
                break
            except Exception as e:
                daemon_logger.error(f"Error: {e}")

            if self._running:
                time.sleep(1)

        self._shutdown()

    def _shutdown(self):
        daemon_logger.info("Shutting down...")
        running_instances = [i for i in self.instances.values() if i.status == "running"]
        if running_instances:
            daemon_logger.info(f"Stopping {len(running_instances)} running instance(s)...")
            for instance in running_instances:
                instance.stop()
        else:
            daemon_logger.info("No running instances to stop")

        self.close()
        daemon_logger.info("Daemon stopped")


def main():
    manager = DaemonManager()

    if os.name != "nt":
        def shutdown_handler(signum, frame):
            daemon_logger.info(f"Received signal {signum}, initiating shutdown...")
            manager._running = False
            for h in daemon_logger.handlers:
                if hasattr(h, 'flush'):
                    h.flush()
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

    manager.run()


if __name__ == "__main__":
    main()