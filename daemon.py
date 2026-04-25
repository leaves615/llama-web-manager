#!/usr/bin/env python3
"""
Llama Server Manager - 守护进程
独立管理 llama-server 进程，重启主应用不影响运行中的实例
"""
import datetime as dt
import json
import os
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
LOG_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
        created_at: str,
    ):
        self.instance_id = instance_id
        self.name = name
        self.executable_path = executable_path
        self.command = command
        self.log_file = log_file
        self.visual_args = visual_args
        self.freeform_args = freeform_args
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

        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        self._start_log_capture()
        print(f"[Daemon] Started instance {self.name} (PID: {self.pid})")

    def stop(self):
        self._stopped_by_manager = True
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
        print(f"[Daemon] Stopped instance {self.name}")

    def _start_log_capture(self):
        def capture():
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"\n[{utc_now_iso()}] === Daemon attached, logging started ===\n")
                f.write(f"[{utc_now_iso()}] command: {' '.join(self.command)}\n")
                f.flush()

                stdout = self.process.stdout
                if stdout is None:
                    return

                for line in stdout:
                    clean = line.rstrip("\n")
                    msg = f"[{utc_now_iso()}] {clean}"
                    f.write(msg + "\n")
                    f.flush()

                return_code = self.process.poll()
                if return_code is None:
                    try:
                        return_code = self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        return_code = -1
                        self.process.kill()

                ended = f"[{utc_now_iso()}] process exited with code {return_code}"
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
        self._db_conn = sqlite3.connect(DB_FILE, timeout=5)
        self._db_conn.row_factory = sqlite3.Row

    def close(self):
        self._running = False
        self._db_conn.close()

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
                print(f"[Daemon] Marked stale instance {instance_id} as stopped")
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
                    created_at=row["created_at"],
                )
                instance._stopped_by_manager = True
                self.instances[instance_id] = instance
                print(f"[Daemon] Restored instance {instance.name} (PID: {pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                print(f"[Daemon] Failed to restore {row['instance_id']}: {e}")

    def process_commands(self):
        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'start'")
        rows = cursor.fetchall()
        print(f"[Daemon] Found {len(rows)} instances with command='start'")
        for row in rows:
            print(f"[Daemon] Processing start for: {row['instance_id']}")
            self._start_instance(row)

        cursor = self._db_execute("SELECT * FROM instances WHERE command = 'stop'")
        rows = cursor.fetchall()
        print(f"[Daemon] Found {len(rows)} instances with command='stop'")
        for row in rows:
            print(f"[Daemon] Processing stop for: {row['instance_id']}")
            self._stop_instance(row)

    def _start_instance(self, row):
        instance_id = row["instance_id"]
        if instance_id in self.instances:
            instance = self.instances[instance_id]
            if instance.status == "running":
                self._db_execute(
                    "UPDATE instances SET command = NULL, updated_at = ? WHERE instance_id = ?",
                    (utc_now_iso(), instance_id),
                )
                return

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
                created_at=row["created_at"],
            )
            instance.start()

            self.instances[instance_id] = instance

            self._db_execute(
                "UPDATE instances SET pid = ?, status = 'running', command = NULL, updated_at = ? WHERE instance_id = ?",
                (instance.pid, utc_now_iso(), instance_id),
            )
            print(f"[Daemon] Started {instance.name}")
        except Exception as e:
            print(f"[Daemon] Failed to start {instance_id}: {e}")
            self._db_execute(
                "UPDATE instances SET status = 'error', command = NULL, updated_at = ? WHERE instance_id = ?",
                (utc_now_iso(), instance_id),
            )

    def _stop_instance(self, row):
        instance_id = row["instance_id"]
        instance = self.instances.get(instance_id)
        if instance:
            instance.stop()
            del self.instances[instance_id]

        self._db_execute(
            "UPDATE instances SET pid = NULL, status = 'stopped', command = NULL, updated_at = ? WHERE instance_id = ?",
            (utc_now_iso(), instance_id),
        )
        print(f"[Daemon] Stopped {instance_id}")

    def check_status(self):
        to_remove = []
        for instance_id, instance in self.instances.items():
            if instance.status == "stopped":
                self._db_execute(
                    "UPDATE instances SET status = 'stopped', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (utc_now_iso(), instance_id),
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
                if proc.poll() is not None:
                    self._db_execute(
                        "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                        (utc_now_iso(), instance_id),
                    )
                    print(f"[Daemon] Instance {instance_id} process exited")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._db_execute(
                    "UPDATE instances SET status = 'exited', pid = NULL, updated_at = ? WHERE instance_id = ?",
                    (utc_now_iso(), instance_id),
                )
                print(f"[Daemon] Instance {instance_id} process not found")

    def run(self):
        print("[Daemon] Starting...")
        self.scan_existing()
        self.load_instances()

        print("[Daemon] Running, press Ctrl+C to stop")

        while self._running:
            try:
                self.process_commands()
                self.check_status()
                time.sleep(1)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Daemon] Error: {e}")
                time.sleep(1)

        print("[Daemon] Stopping all instances...")
        for instance in list(self.instances.values()):
            instance.stop()

        self.close()
        print("[Daemon] Stopped")


def main():
    manager = DaemonManager()
    manager.run()


if __name__ == "__main__":
    main()