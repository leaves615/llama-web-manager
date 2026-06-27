#!/usr/bin/env python3
"""
TCP 协议测试套件

测试 app.py 和 daemon.py 之间的 TCP 通信逻辑
"""
import json
import socket
import time
import subprocess
import sys
import os
import signal
from pathlib import Path
from typing import Optional, Dict

# 配置
APP_ROOT = Path(__file__).parent
DAEMON_PID_FILE = APP_ROOT / "daemon.pid"
TEST_TIMEOUT = 10


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
        pid = int(lines[0].strip())
        port = int(lines[1].strip())
        return pid, port
    except Exception:
        return None


def _control_request(request: Dict, timeout: float = 10) -> Optional[Dict]:
    """向 daemon 发送 TCP 控制请求"""
    info = _get_daemon_info()
    if not info:
        return None

    daemon_pid, port = info

    try:
        import psutil
        proc = psutil.Process(daemon_pid)
        if not proc.is_running():
            return None
    except Exception:
        return None

    attempts = 3
    for attempt in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(("127.0.0.1", port))
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                
                # 读取响应
                buffer = b''
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    if b'\n' in buffer:
                        line = buffer.split(b'\n', 1)[0]
                        return json.loads(line.decode("utf-8"))
        except Exception as e:
            if attempt == attempts - 1:
                print(f"TCP request failed: {e}")
                return None
            time.sleep(0.2 * (attempt + 1))
    return None


def start_daemon():
    """启动 daemon 进程"""
    daemon_path = APP_ROOT / "daemon.py"
    proc = subprocess.Popen(
        [sys.executable, str(daemon_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    
    # 等待 daemon 就绪
    for _ in range(20):
        time.sleep(0.5)
        info = _get_daemon_info()
        if info:
            pid, port = info
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    if sock.connect_ex(("127.0.0.1", port)) == 0:
                        print(f"✓ Daemon started (PID: {pid}, Port: {port})")
                        return True
            except Exception:
                pass
    return False


def stop_daemon():
    """停止 daemon 进程"""
    response = _control_request({"action": "stop", "target": "daemon"}, timeout=10)
    if response and response.get("success"):
        # 等待 daemon 退出
        for _ in range(20):
            time.sleep(0.5)
            if not _get_daemon_info():
                print("✓ Daemon stopped")
                return True
    return False


def test_tcp_connection():
    """测试 1: TCP 连接"""
    print("\n=== 测试 1: TCP 连接 ===")
    
    # 测试 daemon 状态查询
    info = _get_daemon_info()
    if not info:
        print("✗ Daemon 未运行")
        return False
    
    pid, port = info
    print(f"  Daemon PID: {pid}, Port: {port}")
    
    # 测试 TCP 连通性
    response = _control_request({"action": "list", "target": "daemon"})
    if not response:
        print("✗ TCP 连接失败")
        return False
    
    print(f"✓ TCP 连接成功，响应: {response}")
    return True


def test_list_instances():
    """测试 2: 列出实例"""
    print("\n=== 测试 2: 列出实例 ===")
    
    response = _control_request({"action": "list", "target": "instance"})
    if not response or not response.get("success"):
        print(f"✗ 列出实例失败: {response}")
        return False
    
    instances = response.get("instances", [])
    print(f"✓ 当前实例数量: {len(instances)}")
    for inst in instances:
        print(f"  - {inst.get('name')} (ID: {inst.get('instance_id')}, Status: {inst.get('status')})")
    
    return True


def test_create_instance():
    """测试 3: 创建实例"""
    print("\n=== 测试 3: 创建实例 ===")
    
    # 查找可用的 llama-server
    test_server_dir = "/Users/eddy/tools/llama.cpp/build/bin"
    if not Path(test_server_dir).exists():
        print(f"⚠ 测试目录不存在: {test_server_dir}，跳过创建测试")
        return True
    
    response = _control_request({
        "action": "create",
        "target": "instance",
        "name": "test-instance",
        "server_dir": test_server_dir,
        "visual_args": {
            "model_path": "",
            "host": "127.0.0.1",
            "port": 8080,
        },
        "freeform_args": "",
        "env_vars": [],
    })
    
    if not response or not response.get("success"):
        print(f"✗ 创建实例失败: {response}")
        return False
    
    instance = response.get("instance", {})
    instance_id = instance.get("instance_id")
    print(f"✓ 实例创建成功: {instance_id}")
    print(f"  Name: {instance.get('name')}")
    print(f"  Status: {instance.get('status')}")
    
    # 保存 instance_id 供后续测试使用
    global TEST_INSTANCE_ID
    TEST_INSTANCE_ID = instance_id
    
    return True


def test_get_instance():
    """测试 4: 获取单个实例"""
    print("\n=== 测试 4: 获取单个实例 ===")
    
    if not TEST_INSTANCE_ID:
        print("⚠ 无测试实例，跳过")
        return True
    
    response = _control_request({
        "action": "get",
        "target": "instance",
        "instance_id": TEST_INSTANCE_ID,
    })
    
    if not response or not response.get("success"):
        print(f"✗ 获取实例失败: {response}")
        return False
    
    instance = response.get("instance", {})
    print(f"✓ 获取实例成功:")
    print(f"  ID: {instance.get('instance_id')}")
    print(f"  Name: {instance.get('name')}")
    print(f"  Status: {instance.get('status')}")
    print(f"  PID: {instance.get('pid')}")
    
    return True


def test_update_instance():
    """测试 5: 更新实例"""
    print("\n=== 测试 5: 更新实例 ===")
    
    if not TEST_INSTANCE_ID:
        print("⚠ 无测试实例，跳过")
        return True
    
    response = _control_request({
        "action": "update",
        "target": "instance",
        "instance_id": TEST_INSTANCE_ID,
        "name": "test-instance-updated",
        "server_dir": "/Users/eddy/tools/llama.cpp/build/bin",
        "visual_args": {
            "model_path": "",
            "host": "127.0.0.1",
            "port": 8081,  # 修改端口
        },
        "freeform_args": "",
        "env_vars": [],
    })
    
    if not response or not response.get("success"):
        print(f"✗ 更新实例失败: {response}")
        return False
    
    instance = response.get("instance", {})
    print(f"✓ 实例更新成功:")
    print(f"  Name: {instance.get('name')}")
    print(f"  Port: {instance.get('visual_args', {}).get('port')}")
    
    return True


def test_delete_instance():
    """测试 6: 删除实例"""
    print("\n=== 测试 6: 删除实例 ===")
    
    if not TEST_INSTANCE_ID:
        print("⚠ 无测试实例，跳过")
        return True
    
    response = _control_request({
        "action": "delete",
        "target": "instance",
        "instance_id": TEST_INSTANCE_ID,
    })
    
    if not response or not response.get("success"):
        print(f"✗ 删除实例失败: {response}")
        return False
    
    print(f"✓ 实例删除成功")
    
    # 验证删除
    response = _control_request({
        "action": "get",
        "target": "instance",
        "instance_id": TEST_INSTANCE_ID,
    })
    
    if response and response.get("success"):
        print("✗ 实例仍存在，删除失败")
        return False
    
    print("✓ 验证删除成功")
    return True


def test_logs_query():
    """测试 7: 日志查询"""
    print("\n=== 测试 7: 日志查询 ===")
    
    if not TEST_INSTANCE_ID:
        print("⚠ 无测试实例，跳过")
        return True
    
    response = _control_request({
        "action": "logs",
        "target": "instance",
        "instance_id": TEST_INSTANCE_ID,
        "lines": 10,
    })
    
    if not response or not response.get("success"):
        print(f"✗ 日志查询失败: {response}")
        return False
    
    lines = response.get("lines", [])
    print(f"✓ 日志查询成功，返回 {len(lines)} 行")
    if lines:
        print(f"  最后一行: {lines[-1]}")
    
    return True


def test_error_handling():
    """测试 8: 错误处理"""
    print("\n=== 测试 8: 错误处理 ===")
    
    # 测试不存在的实例
    response = _control_request({
        "action": "get",
        "target": "instance",
        "instance_id": "non-existent-id",
    })
    
    if response and response.get("success"):
        print("✗ 应该返回错误，但返回了成功")
        return False
    
    print(f"✓ 不存在的实例正确返回错误: {response}")
    
    # 测试无效的命令
    response = _control_request({
        "action": "invalid_action",
        "target": "instance",
    })
    
    if response and response.get("success"):
        print("✗ 应该返回错误，但返回了成功")
        return False
    
    print(f"✓ 无效命令正确返回错误: {response}")
    
    return True


def test_daemon_restart():
    """测试 9: daemon 重启"""
    print("\n=== 测试 9: daemon 重启 ===")
    
    # 停止 daemon
    if not stop_daemon():
        print("✗ 停止 daemon 失败")
        return False
    
    time.sleep(1)
    
    # 验证 daemon 已停止
    if _get_daemon_info():
        print("✗ daemon 仍在运行")
        return False
    
    print("✓ daemon 已停止")
    
    # 重新启动 daemon
    if not start_daemon():
        print("✗ 重启 daemon 失败")
        return False
    
    # 验证 TCP 连接恢复
    if not test_tcp_connection():
        print("✗ TCP 连接未恢复")
        return False
    
    print("✓ daemon 重启成功，TCP 连接恢复")
    return True


# 全局测试实例 ID
TEST_INSTANCE_ID = None


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("TCP 协议测试套件")
    print("=" * 60)
    
    # 确保 daemon 运行
    if not _get_daemon_info():
        print("\nDaemon 未运行，正在启动...")
        if not start_daemon():
            print("✗ 启动 daemon 失败")
            return False
    
    results = []
    
    # 运行测试
    tests = [
        ("TCP 连接", test_tcp_connection),
        ("列出实例", test_list_instances),
        ("创建实例", test_create_instance),
        ("获取实例", test_get_instance),
        ("更新实例", test_update_instance),
        ("日志查询", test_logs_query),
        ("删除实例", test_delete_instance),
        ("错误处理", test_error_handling),
        ("daemon 重启", test_daemon_restart),
    ]
    
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"✗ {name} 测试异常: {e}")
            results.append((name, False))
    
    # 打印结果汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status}: {name}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
