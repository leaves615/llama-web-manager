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
import atexit


APP_ROOT = Path(__file__).parent
LOG_DIR = APP_ROOT / "logs"
DB_FILE = APP_ROOT / "instances.db"
CONFIG_FILE = APP_ROOT / "config.yaml"
DAEMON_LOG_FILE = LOG_DIR / "daemon.log"
DAEMON_PID_FILE = APP_ROOT / "daemon.pid"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_pid_file():
    try:
        if DAEMON_PID_FILE.exists():
            DAEMON_PID_FILE.unlink()
    except Exception:
        pass


atexit.register(_cleanup_pid_file)


def find_free_port():
    """获取随机可用端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        return s.getsockname()[1]


def write_daemon_pid(port: int):
    """写入 daemon PID 和端口信息"""
    # 使用临时文件并原子替换，避免部分写入导致读取方解析失败
    import tempfile
    dirpath = str(APP_ROOT)
    fd, tmp = tempfile.mkstemp(dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{port}\n")
        os.replace(tmp, str(DAEMON_PID_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def read_daemon_info():
    """读取 daemon PID 和端口信息"""
    if not DAEMON_PID_FILE.exists():
        return None, None
    try:
        with open(DAEMON_PID_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) >= 2:
            try:
                pid = int(lines[0].strip())
                port = int(lines[1].strip())
                return pid, port
            except ValueError:
                return None, None
    except Exception:
        return None, None
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


def _safe_terminate(proc: psutil.Process) -> bool:
    """安全地终止进程，返回是否成功"""
    try:
        proc.terminate()
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _safe_kill(proc: psutil.Process) -> bool:
    """安全地杀死进程，返回是否成功"""
    try:
        proc.kill()
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process_tree(pid: int, include_parent: bool = True) -> bool:
    """跨平台终止进程树，返回是否成功"""
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True

    # 终止所有子进程
    try:
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []

    for child in children:
        _safe_terminate(child)

    gone, alive = psutil.wait_procs(children, timeout=3)
    for p in alive:
        _safe_kill(p)
    if alive:
        gone, alive = psutil.wait_procs(alive, timeout=2)
        for p in alive:
            daemon_logger.warning(f"子进程 {p.pid} 仍未退出，已尽力终止")

    # 终止父进程
    if include_parent:
        try:
            parent.terminate()
            try:
                parent.wait(timeout=3)
            except psutil.TimeoutExpired:
                _safe_kill(parent)
                try:
                    parent.wait(timeout=2)
                except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    try:
        return not _pid_alive(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def _pid_alive(pid: int) -> bool:
    """进程是否仍存活（僵尸态视为已退出，避免误判残留）"""
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        try:
            if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                return False
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _send_response(conn: socket.socket, obj: Dict) -> None:
    """发送换行结尾的 JSON 响应（客户端按行读取，缺换行会导致控制请求超时）"""
    conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _recv_request(conn: socket.socket, limit: int = 65536) -> str:
    """读取到换行符为止的完整请求，避免大于 4096 字节时被截断"""
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            break
    return buf.decode("utf-8", errors="replace").strip()


def now_iso():
    return dt.datetime.now().astimezone().isoformat()


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
        try:
            return self.process.pid if self.process else 0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def _process_alive(self) -> bool:
        """内存中的进程对象是否仍存活（Popen 走 poll，psutil 走存活判断）"""
        proc = self.process
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        if callable(poll):
            try:
                return proc.poll() is None
            except Exception:
                return False
        try:
            return _pid_alive(proc.pid)
        except Exception:
            return False

    @property
    def status(self) -> str:
        if self._stopped_by_manager:
            return "stopped"
        if self.process is None:
            return "stopped"
        if self._process_alive():
            return "running"
        try:
            code = self.process.returncode
        except Exception:
            code = None
        return f"exited({code})" if code is not None else "exited"

    def start(self):
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            new_session = True

        env = os.environ.copy()
        for item in self.env_vars:
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

        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=new_session if os.name != "nt" else False,
            env=env,
        )

        self._start_log_capture()
        daemon_logger.info(f"Started instance {self.name} (PID: {self.pid})")

    def stop(self) -> bool:
        self._stopped_by_manager = True
        pid = self.pid
        daemon_logger.info(f"Stopping instance {self.name} (PID: {pid})...")
        if pid == 0:
            daemon_logger.warning(f"Instance {self.name} has no PID")
            return False

        success = kill_process_tree(pid, include_parent=True)
        if not success:
            try:
                parent = psutil.Process(pid)
                if parent.is_running():
                    daemon_logger.warning(f"Process still alive after kill_process_tree, trying direct SIGKILL...")
                    _safe_kill(parent)
                    try:
                        parent.wait(timeout=2)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                        pass
            except psutil.NoSuchProcess:
                return True

        # 最终验证（僵尸态视为已退出）
        if _pid_alive(pid):
            daemon_logger.warning(f"Instance {self.name} (PID: {pid}) may still be running")
            self._stopped_by_manager = False
            return False

        daemon_logger.info(f"Stopped instance {self.name}")
        return True

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

                return_code = None
                try:
                    return_code = self.process.poll()
                    if return_code is None:
                        try:
                            return_code = self.process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            return_code = -1
                            self.process.kill()
                except AttributeError as e:
                    daemon_logger.error(f"Error polling process: {e}, process type: {type(self.process)}")
                    return_code = -999

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
                        data = _recv_request(conn)
                        if not data:
                            _send_response(conn, {"success": False, "error": "empty request"})
                            conn.close()
                            continue

                        daemon_logger.debug(f"Received control command: {data[:100]}")
                        request = json.loads(data)
                        action = request.get("action")
                        target = request.get("target")

                        if target is None:
                            _send_response(conn, {"success": False, "error": "target is required"})
                            conn.close()
                            continue

                        if action == "stop" and target == "daemon":
                            _send_response(conn, {"success": True})
                            conn.close()
                            daemon_logger.info("Control server received stop command")
                            self._running = False
                            return

                        if action == "list" and target == "daemon":
                            _send_response(conn, {
                                "success": True,
                                "status": "running",
                                "instances": len(self.instances)
                            })
                            conn.close()
                            continue

                        response = self._handle_command(request)
                        _send_response(conn, response)
                        conn.close()
                    except json.JSONDecodeError as e:
                        _send_response(conn, {"success": False, "error": f"invalid JSON: {e}"})
                        conn.close()
                    except Exception as e:
                        daemon_logger.error(f"Control server error: {e}")
                        try:
                            _send_response(conn, {"success": False, "error": str(e)})
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

    def _matches_expected_command(self, proc: psutil.Process, expected_command: List[str]) -> bool:
        """校验 PID 是否为本实例的进程：只比对可执行文件名，避免编辑改参后误判为残留"""
        if not expected_command:
            return True
        try:
            actual_command = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False
        if not actual_command:
            return False
        try:
            expected_exe = Path(expected_command[0]).name
            actual_exe = Path(actual_command[0]).name
        except Exception:
            return False
        return actual_exe == expected_exe

    def _serving_port(self, row: Dict) -> Optional[int]:
        """从实例配置中解析服务端口，探测不到则返回 None（不影响存活判断）"""
        try:
            visual = json.loads(row.get("visual_args_json", "") or "") if row.get("visual_args_json") else {}
            port = (visual or {}).get("port")
            return int(port) if port else None
        except (ValueError, TypeError):
            return None

    def _probe_persisted_instance(self, row: Dict, instance: Optional["LlamaInstance"] = None) -> tuple[str, Optional[int]]:
        # Convert sqlite3.Row to dict for .get() compatibility
        if not isinstance(row, dict):
            row = dict(row)

        status = row["status"]
        pid = row["pid"]

        if instance is not None:
            try:
                mem_status = instance.status
            except Exception:
                mem_status = "stopped"
            if mem_status == "running":
                return "running", instance.pid
            if mem_status.startswith("exited"):
                self._db_execute(
                    "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (now_iso(), row["instance_id"]),
                )
                return "exited", None

        if status != "running":
            return status, pid

        if not pid:
            self._db_execute(
                "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                (now_iso(), row["instance_id"]),
            )
            return "exited", None

        expected_command = json.loads(row.get("command_json", "") or "") if row.get("command_json") else []
        try:
            if not _pid_alive(pid):
                raise psutil.NoSuchProcess(pid)
            if not self._matches_expected_command(psutil.Process(pid), expected_command):
                raise psutil.NoSuchProcess(pid)
            port = self._serving_port(row)
            if port and not self._port_serving(port):
                return "starting", pid
            return "running", pid
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            self._db_execute(
                "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                (now_iso(), row["instance_id"]),
            )
            return "exited", None

    @staticmethod
    def _port_serving(port: int, timeout: float = 0.5) -> bool:
        """端口是否已有服务在监听（区分 starting/running，避免按钮提前显示停止）"""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

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

        status, pid = self._probe_persisted_instance(row, instance)
        result["status"] = status
        result["pid"] = pid

        return result

    def _do_start_instance(self, instance_id: str) -> Dict:
        """启动单个实例（通过 TCP 请求触发）"""
        if not instance_id:
            return {"success": False, "error": "instance_id required"}

        row = self._get_persisted_instance(instance_id)
        if not row:
            return {"success": False, "error": "instance not found"}

        # 内存中的实例视为“需要先停后起”的重启请求：必须先停掉旧进程
        if instance_id in self.instances:
            inst = self.instances[instance_id]
            daemon_logger.info(f"Instance {inst.name} is {inst.status}, stopping old process before start...")
            try:
                ok = inst.stop()
            except Exception as e:
                daemon_logger.warning(f"Error stopping old instance {instance_id}: {e}")
                ok = False
            if _pid_alive(inst.pid):
                return {"success": False, "error": f"旧进程 PID {inst.pid} 仍在运行，拒绝启动新进程"}
            self.instances.pop(instance_id, None)

        # DB 级防护：DB 显示 running 但内存无实例，说明旧进程可能残留，先按 PID 终止
        db_inst = self._get_persisted_instance(instance_id)
        old_pid = (db_inst or {}).get("pid") or 0
        if old_pid and _pid_alive(old_pid):
            daemon_logger.info(f"Instance {instance_id} 残留旧进程 PID {old_pid}，先终止再启动")
            kill_process_tree(old_pid, include_parent=True)
            if _pid_alive(old_pid):
                return {"success": False, "error": f"旧进程 PID {old_pid} 仍在运行，拒绝启动新进程"}

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
                "UPDATE instances SET pid = ?, status = 'running', updated_at = ? WHERE instance_id = ?",
                (instance.pid, now_iso(), instance_id),
            )
            daemon_logger.info(f"Started {instance.name}")
            return {"success": True, "instance": self._serialize_instance(instance_id, instance)}
        except Exception as e:
            daemon_logger.error(f"Failed to start {instance_id}: {e}")
            self._db_execute(
                "UPDATE instances SET status = 'error', updated_at = ? WHERE instance_id = ?",
                (now_iso(), instance_id),
            )
            return {"success": False, "error": str(e)}

    def _do_stop_instance(self, instance_id: str) -> Dict:
        """停止单个实例：先确认进程真正退出，才更新 DB，避免新旧进程并存"""
        if not instance_id:
            return {"success": False, "error": "instance_id required"}

        db_row = self._get_persisted_instance(instance_id)
        if not db_row:
            return {"success": False, "error": "instance not found"}
        if db_row.get("status") != "running":
            return {"success": True, "instance": self._serialize_instance(instance_id)}

        instance = self.instances.get(instance_id)
        stopped = False
        pid = (instance.pid if instance else 0) or db_row.get("pid") or 0

        # 1. 先尝试 kill 进程（内存中或根据 PID）
        if instance:
            try:
                stopped = instance.stop()
            except Exception as e:
                daemon_logger.error(f"Failed to stop instance {instance_id}: {e}")
                stopped = False
            if stopped or not _pid_alive(pid):
                self.instances.pop(instance_id, None)
        elif pid:
            # 实例不在内存中，但 DB 显示 running — 尝试根据 PID 终止
            daemon_logger.info(f"Instance {db_row['name']} not in memory, killing PID {pid} directly")
            stopped = kill_process_tree(pid, include_parent=True)

        # 2. 以进程真实存活为准：还活着就如实报错，不更新 DB
        if pid and _pid_alive(pid):
            daemon_logger.error(f"实例 {instance_id} (PID: {pid}) 停止失败，进程仍存活")
            return {"success": False, "error": f"进程 PID {pid} 仍在运行，停止失败"}

        self.instances.pop(instance_id, None)
        self._db_execute(
            "UPDATE instances SET status = 'stopped', pid = NULL, updated_at = ? WHERE instance_id = ?",
            (now_iso(), instance_id),
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
        """daemon 启动时清理 DB 中的脏 running 记录：PID 已不存在才标 stopped"""
        cursor = self._db_execute("SELECT * FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            instance_id = row["instance_id"]
            if not pid or not _pid_alive(pid):
                self._db_execute(
                    "UPDATE instances SET status = 'stopped', pid = NULL WHERE instance_id = ?",
                    (instance_id,),
                )
                daemon_logger.info(f"Marked stale instance {instance_id} as stopped")

    def load_instances(self):
        """daemon 启动时接管存活的旧进程：用 Popen 接管 PID，避免 stop 时找不到进程对象"""
        cursor = self._db_execute("SELECT * FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            if not pid or not _pid_alive(pid):
                continue
            try:
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
                instance.process = psutil.Process(pid)
                self.instances[instance_id] = instance
                daemon_logger.info(f"接管存活实例 {instance.name} (PID: {pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as e:
                daemon_logger.error(f"Failed to restore {row['instance_id']}: {e}")

    def check_status(self):
        to_remove = []
        for instance_id, instance in list(self.instances.items()):
            try:
                if instance.status == "stopped":
                    self._db_execute(
                        "UPDATE instances SET status = 'stopped', pid = NULL, updated_at = ? WHERE instance_id = ?",
                        (now_iso(), instance_id),
                    )
                    to_remove.append(instance_id)
            except Exception as e:
                daemon_logger.error(f"Error checking status for instance {instance_id}: {e}")
                to_remove.append(instance_id)

        for instance_id in to_remove:
            try:
                del self.instances[instance_id]
            except KeyError:
                pass

        cursor = self._db_execute("SELECT instance_id, pid FROM instances WHERE status = 'running'")
        rows = cursor.fetchall()
        for row in rows:
            pid = row["pid"]
            if not pid:
                continue
            instance_id = row["instance_id"]
            try:
                # 内存中有对象且已退出：以内存状态为准落库
                inst = self.instances.get(instance_id)
                if inst is not None:
                    try:
                        mem_status = inst.status
                    except Exception:
                        mem_status = "stopped"
                    if mem_status.startswith("exited"):
                        self._db_execute(
                            "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                            (now_iso(), instance_id),
                        )
                        daemon_logger.warning(f"Instance {instance_id} process exited ({mem_status})")
                        del self.instances[instance_id]
                        continue
                if not _pid_alive(pid):
                    raise psutil.NoSuchProcess(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._db_execute(
                    "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (now_iso(), instance_id),
                )
                daemon_logger.warning(f"Instance {instance_id} process not found")
                # 清理内存中的已退出实例对象，避免重启时卡住
                if instance_id in self.instances:
                    del self.instances[instance_id]
            except Exception as e:
                daemon_logger.error(f"Error checking process {instance_id} (pid={pid}): {e}")

    def run(self):
        def do_shutdown():
            daemon_logger.info("Received shutdown signal")
            self._running = False

        if os.name != "nt":
            signal.signal(signal.SIGTERM, lambda s, f: do_shutdown())
            signal.signal(signal.SIGINT, lambda s, f: do_shutdown())

        self._start_control_server()
        daemon_logger.info("Starting daemon...")
        try:
            self.scan_existing()
        except Exception as e:
            daemon_logger.error(f"Error during scan_existing: {e}")
        
        try:
            self.load_instances()
        except Exception as e:
            daemon_logger.error(f"Error during load_instances: {e}")

        daemon_logger.info("Daemon running, press Ctrl+C to stop")

        while self._running:
            try:
                self.check_status()
            except KeyboardInterrupt:
                daemon_logger.info("Received interrupt signal")
                break
            except Exception as e:
                daemon_logger.error(f"Error in daemon loop: {type(e).__name__}: {e}", exc_info=False)

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