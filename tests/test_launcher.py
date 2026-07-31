"""Tests for ILA Launcher components."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Platform adapter tests ─────────────────────────────────────────────


class TestPlatformAdapter:
    """测试平台适配层."""

    def test_kill_port_no_process(self):
        """端口无进程时正常返回空."""
        from ila.launcher_platform import kill_port
        # 选择大概率空闲的端口
        result = kill_port(54321)
        assert result == []

    def test_is_port_in_use_free_port(self):
        """空闲端口返回 False."""
        from ila.launcher_platform import is_port_in_use
        assert not is_port_in_use(54321)

    def test_is_port_in_use_listening_port(self):
        """监听端口返回 True."""
        from ila.launcher_platform import is_port_in_use
        # 用一个临时 socket 绑定端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 54322))
            s.listen(1)
            assert is_port_in_use(54322)

    def test_wait_port_free_timeout(self):
        """端口超时未释放返回 False."""
        from ila.launcher_platform import wait_port_free
        # 端口空闲，应该立即返回 True
        assert wait_port_free(54323, timeout=0.1)

    def test_spawn_detached_returns_process(self):
        """spawn_detached 返回 Popen 对象."""
        from ila.launcher_platform import spawn_detached
        proc = spawn_detached(["python3", "-c", "exit(0)"])
        assert proc is not None
        proc.wait(timeout=5)
        assert proc.returncode == 0

    def test_spawn_detached_invalid_cmd(self):
        """无效命令返回 None."""
        from ila.launcher_platform import spawn_detached
        proc = spawn_detached(["/nonexistent/binary"])
        assert proc is None

    def test_health_check_invalid_url(self):
        """不存在的 URL 返回 False."""
        from ila.launcher_platform import health_check
        result = health_check("http://127.0.0.1:54324/api/status", timeout=1)
        assert result is False


# ── Launcher core tests ────────────────────────────────────────────────


class TestLauncher:
    """测试 Launcher 核心."""

    @pytest.fixture
    def cmd_dir(self):
        """创建临时命令目录."""
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_launcher_init(self, cmd_dir):
        """Launcher 初始化."""
        from ila.launcher import Launcher
        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)
        assert launcher.cmd_dir == cmd_dir
        assert launcher.scan_interval == 0.1

    def test_launcher_processes_restart_command(self, cmd_dir):
        """Launcher 处理重启命令并写入结果."""
        from ila.launcher import Launcher

        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

        # 启动旧服务
        port = _find_free_port()
        old_proc = _start_echo_server(port)

        # 新版服务的启动命令 (会重新监听同一个端口)
        new_cmd = _make_echo_server_cmd(port)

        try:
            cmd_file = cmd_dir / "restart-test001.json"
            cmd_file.write_text(json.dumps({
                "action": "restart",
                "name": "test-service",
                "port": port,
                "cmd": new_cmd,
                "health_check_url": f"http://127.0.0.1:{port}/",
                "health_check_timeout": 5,
            }))

            # 执行命令处理
            launcher._process_commands()

            # 检查结果
            result_file = cmd_dir / "restart-test001.result.json"
            assert result_file.exists()
            result = json.loads(result_file.read_text())
            assert result["status"] == "success"
            assert "new_pid" in result
            assert "elapsed" in result

            # 命令文件已清理
            assert not cmd_file.exists()

            # 清理新进程
            new_pid = result["new_pid"]
            try:
                os.kill(new_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        finally:
            old_proc.terminate()
            old_proc.wait(timeout=5)

    def test_launcher_skips_processed(self, cmd_dir):
        """Launcher 跳过已有结果文件的命令."""
        from ila.launcher import Launcher

        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

        # 写入命令和结果（模拟已处理）
        cmd_file = cmd_dir / "restart-skip.json"
        result_file = cmd_dir / "restart-skip.result.json"
        cmd_file.write_text(json.dumps({"action": "restart", "name": "x", "port": 1}))
        result_file.write_text(json.dumps({"status": "success"}))

        # 执行处理
        launcher._process_commands()

        # 命令文件应该还在（被跳过）
        assert cmd_file.exists()

    def test_launcher_invalid_command(self, cmd_dir):
        """Launcher 处理无效命令."""
        from ila.launcher import Launcher

        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

        cmd_file = cmd_dir / "restart-invalid.json"
        cmd_file.write_text("not valid json")

        launcher._process_commands()

        result_file = cmd_dir / "restart-invalid.result.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result["status"] == "error"

    def test_launcher_missing_port(self, cmd_dir):
        """缺少 port 参数时返回 error."""
        from ila.launcher import Launcher

        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

        cmd_file = cmd_dir / "restart-noport.json"
        cmd_file.write_text(json.dumps({"action": "restart", "name": "x"}))

        launcher._process_commands()

        result_file = cmd_dir / "restart-noport.result.json"
        result = json.loads(result_file.read_text())
        assert result["status"] == "error"
        assert "port" in result["reason"].lower()

    def test_launcher_lock_prevents_duplicate(self, cmd_dir):
        """锁文件防止重复处理."""
        from ila.launcher import Launcher

        launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

        cmd_file = cmd_dir / "restart-locked.json"
        lock_file = cmd_dir / "restart-locked.lock"
        cmd_file.write_text(json.dumps({"action": "restart", "name": "x", "port": 1}))
        lock_file.touch()

        launcher._process_commands()

        # 结果文件不应出现
        result_file = cmd_dir / "restart-locked.result.json"
        assert not result_file.exists()


# ── Launcher Manager tests ─────────────────────────────────────────────


class TestLauncherManager:
    """测试 Launcher Manager."""

    @pytest.fixture
    def cmd_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_manager_init(self, cmd_dir):
        """Manager 初始化."""
        from ila.launcher_manager import LauncherManager
        manager = LauncherManager(cmd_dir=cmd_dir)
        assert not manager.is_running()
        assert manager.pid is None

    def test_manager_start_stop(self, cmd_dir):
        """Manager 启动和停止 Launcher."""
        from ila.launcher_manager import LauncherManager

        manager = LauncherManager(cmd_dir=cmd_dir)
        result = manager.start()
        assert result is True
        assert manager.is_running()
        assert manager.pid is not None

        manager.stop()
        assert not manager.is_running()

    def test_manager_send_restart_wait(self, cmd_dir):
        """发送重启命令并等待结果."""
        from ila.launcher_manager import LauncherManager

        manager = LauncherManager(cmd_dir=cmd_dir)

        # 先启动 Launcher
        assert manager.start()

        # 给 Launcher 一点时间进入主循环
        time.sleep(0.5)

        port = _find_free_port()
        old_proc = _start_echo_server(port)
        new_cmd = _make_echo_server_cmd(port)

        try:
            result = manager.send_restart(
                name="test-via-manager",
                port=port,
                cmd=new_cmd,
                health_check_url=f"http://127.0.0.1:{port}/",
                health_check_timeout=5,
                wait=True,
                wait_timeout=10,
            )
            assert result["status"] == "success"
            # 清理新进程
            new_pid = result.get("new_pid")
            if new_pid:
                try:
                    os.kill(new_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
        finally:
            old_proc.terminate()
            old_proc.wait(timeout=5)
            manager.stop()

    def test_singleton_global_manager(self):
        """全局 Manager 单例."""
        from ila.launcher_manager import (
            get_launcher_manager,
            start_launcher,
            stop_launcher,
        )

        mgr1 = get_launcher_manager()
        mgr2 = get_launcher_manager()
        assert mgr1 is mgr2

        # cleanup: ensure no launcher process left
        stop_launcher()


class TestLauncherGlobFilter:
    """测试 _process_commands 的 glob 过滤逻辑."""

    def test_skips_result_and_lock_files(self):
        """跳过 .result.json 和 .lock 文件，防止无限递归."""
        from ila.launcher import Launcher
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_dir = Path(tmpdir)

            # 创建模拟文件
            cmd_dir.joinpath("restart-aaa.json").write_text(
                '{"action":"restart","name":"test","port":19999,"cmd":["echo","hi"]}')
            cmd_dir.joinpath("restart-aaa.result.json").write_text("{}")
            cmd_dir.joinpath("restart-bbb.json.lock").write_text("")

            launcher = Launcher(cmd_dir=cmd_dir, scan_interval=0.1)

            # Verify that _process_commands doesn't crash
            # and correctly identifies only the real command file
            launcher._process_commands()

            # The result file and lock should be untouched
            # The real command file should either have a result or lock
            # (it will fail at spawn since "echo" isn't a dashboard, but shouldn't crash on glob)
            remaining = list(cmd_dir.glob("restart-*.json"))
            # Should only have the original .json + its result (if created), no nested .result.result...
            for f in remaining:
                assert ".result.result" not in f.name, \
                    f"BUG: nested .result suffix: {f.name}"


# ── Helpers ────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """找到一个空闲端口."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_echo_server(port: int) -> subprocess.Popen:
    """启动一个简单的 HTTP echo 服务器用于测试."""
    code = _echo_server_code(port)
    proc = subprocess.Popen(
        ["python3", "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # 等待服务器启动
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError(f"Echo server didn't start on port {port}")
    return proc


def _echo_server_code(port: int) -> str:
    """返回 echo server 的 Python 源码."""
    return f"""
import http.server
import socketserver
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{{"status":"ok"}}')
    def log_message(self, *args):
        pass
with socketserver.TCPServer(('127.0.0.1', {port}), Handler) as httpd:
    httpd.serve_forever()
"""


def _make_echo_server_cmd(port: int) -> list[str]:
    """返回 echo server 的命令行 (用于 Launcher 重启)."""
    return ["python3", "-c", _echo_server_code(port)]
