"""ILA Launcher Manager — ILA 侧的总管，负责 spawn、发命令、读结果."""

# SKILL.md: 技能配置文件格式，定义技能元数据与行为规范
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import ila.launcher_platform as plat

logger = logging.getLogger(__name__)

DEFAULT_CMD_DIR = Path.home() / ".ila" / "commands"
DEFAULT_LAUNCHER_LOG = Path.home() / ".ila" / "launcher.log"


class LauncherManager:
    """管理 Launcher 进程生命周期，提供发送重启命令的便捷接口."""


    def __init__(
        self,
        cmd_dir: Path | None = None,
        launcher_log: Path | None = None,
    ):
        self.cmd_dir = cmd_dir or DEFAULT_CMD_DIR
        self.launcher_log = launcher_log or DEFAULT_LAUNCHER_LOG
        self._proc: subprocess.Popen | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self, launcher_module: str = "ila.launcher") -> bool:
        """启动 Launcher 进程.

        Args:
            launcher_module: 用 python -m 运行的模块名

        Returns:
            True 如果成功启动
        """
        if self.is_running():
            logger.warning("Launcher 已在运行")
            return True

        self.cmd_dir.mkdir(parents=True, exist_ok=True)
        self.launcher_log.parent.mkdir(parents=True, exist_ok=True)

        # 打开日志文件
        log_fp = open(str(self.launcher_log), "a")

        # 传递 cmd_dir 给子进程
        env = os.environ.copy()
        env["ILA_LAUNCHER_CMD_DIR"] = str(self.cmd_dir)

        try:
            self._proc = subprocess.Popen(
                ["python3", "-m", launcher_module],
                stdout=log_fp,
                stderr=log_fp,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,  # 脱离终端，ILA 死我不死
            )
        except Exception as e:
            logger.error("启动 Launcher 失败: %s", e)
            log_fp.close()
            return False

        logger.info("Launcher 已启动: pid=%d log=%s", self._proc.pid, self.launcher_log)
        return True

    def stop(self) -> None:
        """停止 Launcher 进程."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            logger.info("Launcher 已停止: pid=%d", self._proc.pid)
        self._proc = None

    def is_running(self) -> bool:
        """检查 Launcher 是否在运行."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """当前 Launcher 进程 PID."""
        return self._proc.pid if self._proc else None

    # ── 命令接口 ──────────────────────────────────────────────────────

    def send_restart(
        self,
        name: str,
        port: int,
        cmd: list[str],
        health_check_url: str | None = None,
        health_check_timeout: float = 30.0,
        staging_port: int | None = None,
        cwd: str | None = None,
        cleanup: dict[str, Any] | None = None,
        objects_auto_refresh: bool | None = None,
        pre_kill_delay: float = 2.0,
        version: str | None = None,
        old_version: str | None = None,
        wait: bool = True,
        wait_timeout: float = 45.0,
        lifecycle_phases: list[dict] | None = None,
    ) -> dict[str, Any]:
        """发送重启命令并等待结果.

        Args:
            name: 服务名称（用于日志）
            port: 目标端口
            cmd: 启动命令（完整命令列表）
            health_check_url: 健康检查 URL
            health_check_timeout: 健康检查超时
            staging_port: 需要清理的 staging 端口
            cwd: 新进程的工作目录
            cleanup: 额外清理操作
            objects_auto_refresh: 纳管对象列表定时轮询刷新模式 (None=使用默认值，页面默认加载第一页)
            version: 目标版本号 (部署成功后回写注册表)
            old_version: 旧版本号 (用于版本迭代报告)
            wait: 是否等待结果
            wait_timeout: 等待结果超时
            lifecycle_phases: 迭代各流程环节信息 (可选). 列表每项包含:
                - phase: 环节名 (需求/开发/测试/验证/上线/回滚)
                - icon: 环节图标
                - detail: 做了什么
                - conclusion: 结论如何
                - status: "done" | "skipped" | "failed" | "empty"

        Returns:
            结果字典:
              {"status": "success", "new_pid": ..., "elapsed": ...}
              {"status": "error", "reason": "..."}
              {"status": "timeout", "reason": "..."}
        """
        command_id = uuid.uuid4().hex[:12]
        cmd_file = self.cmd_dir / f"restart-{command_id}.json"
        result_file = self.cmd_dir / f"restart-{command_id}.result.json"

        # 清除可能残留的旧结果
        result_file.unlink(missing_ok=True)

        # 构造命令
        command: dict[str, Any] = {
            "action": "restart",
            "name": name,
            "port": port,
            "cmd": cmd,
        }
        if version:
            command["version"] = version
        if old_version:
            command["old_version"] = old_version
        if cwd:
            command["cwd"] = cwd
        if health_check_url:
            command["health_check_url"] = health_check_url
            command["health_check_timeout"] = health_check_timeout
        if staging_port:
            command["staging_port"] = staging_port
        if cleanup:
            command["cleanup"] = cleanup
        if objects_auto_refresh is not None:
            command["objects_auto_refresh"] = objects_auto_refresh
        if pre_kill_delay is not None:
            command["pre_kill_delay"] = pre_kill_delay
        if lifecycle_phases:
            command["lifecycle_phases"] = lifecycle_phases

        # 写入命令文件
        logger.info("发送命令: id=%s name=%s port=%s version=%s", command_id, name, port, version)
        cmd_file.write_text(json.dumps(command, ensure_ascii=False))

        if not wait:
            return {"status": "dispatched", "command_id": command_id}

        # 等待结果
        timeout = wait_timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result_file.exists():
                try:
                    result = json.loads(result_file.read_text())
                    logger.info(
                        "收到结果: id=%s status=%s", command_id, result.get("status"),
                    )
                    # 清理结果文件
                    result_file.unlink(missing_ok=True)
                    return result
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("读取结果文件失败: %s", e)
                    # 损坏的结果文件，删除并继续等待
                    result_file.unlink(missing_ok=True)

            time.sleep(0.3)

        logger.error("等待结果超时: id=%s timeout=%.1fs", command_id, timeout)
        result_file.unlink(missing_ok=True)
        return {"status": "timeout", "reason": f"等待结果超时 ({timeout}s)"}
# ── 单例 ──────────────────────────────────────────────────────────────

_global_manager: LauncherManager | None = None


def get_launcher_manager() -> LauncherManager:
    """获取全局 LauncherManager 单例."""
    global _global_manager
    if _global_manager is None:
        _global_manager = LauncherManager()
    return _global_manager


def start_launcher() -> bool:
    """便捷函数: 启动全局 Launcher."""
    return get_launcher_manager().start()


def stop_launcher() -> None:
    """便捷函数: 停止全局 Launcher."""
    global _global_manager
    if _global_manager is not None:
        _global_manager.stop()
        _global_manager = None


def restart_via_launcher(
    name: str,
    port: int,
    cmd: list[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """便捷函数: 通过 Launcher 重启服务.

    支持 **kwargs 透传至 send_restart，包括:
      - objects_auto_refresh: 纳管对象定时轮询刷新模式
      - health_check_url / health_check_timeout
      - staging_port / cwd / cleanup
      - version: 目标版本号 (部署成功后回写注册表)
      - pre_kill_delay: 杀旧进程前延迟 (默认 2.0s)
      - wait / wait_timeout
    """
    return get_launcher_manager().send_restart(name, port, cmd, **kwargs)
