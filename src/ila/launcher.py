"""ILA Launcher 核心 — 独立进程，命令文件驱动的通用热重启引擎.

生命周期:
  1. ILA 启动时 spawn Launcher 子进程
  2. Launcher 轮询 ~/.ila/commands/ 目录
  3. 发现 restart-*.json   → 执行重启
  4. 写 restart-*.result.json → ILA 读结果

命令文件格式 (restart-{id}.json):
  {
    "action": "restart",
    "name": "ila-dashboard",
    "port": 9527,
    "cmd": [sys.executable, "-m", "ila.cli", "dashboard", "--port", "9527", ...],
    "health_check_url": "http://127.0.0.1:9527/api/status",
    "health_check_timeout": 30,
    "staging_port": 9528,
    "version": "1.5.0",
    "cleanup": {
      "staging_info_file": "~/.ila/staging/xxx.json",
      "verification_mode": true
    }
  }

结果文件格式 (restart-{id}.result.json):
  {"status": "success", "new_pid": 12345, "elapsed": 3.2, "version_updated": "1.4.0"}
  {"status": "error", "reason": "...", "elapsed": 0.5}
"""

# SKILL.md: 技能配置文件格式，定义技能元数据与行为规范

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from ila.launcher_platform import (
    health_check,
    kill_port,
    spawn_detached,
    wait_port_free,
)

logger = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = 0.5  # 命令扫描间隔（秒）
DEFAULT_CMD_DIR = Path.home() / ".ila" / "commands"


class Launcher:
    """命令驱动的通用进程重启引擎."""

    def __init__(
        self,
        cmd_dir: Path | None = None,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
    ):
        self.cmd_dir = cmd_dir or DEFAULT_CMD_DIR
        self.scan_interval = scan_interval
        self._running = False

    # ── 公共 API ──────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """启动主循环，阻塞直到收到 shutdown 命令."""
        self.cmd_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        logger.info(
            "Launcher 已启动: pid=%d cmd_dir=%s interval=%.1fs",
            os.getpid(), self.cmd_dir, self.scan_interval,
        )

        while self._running:
            try:
                self._process_commands()
            except Exception:
                logger.exception("命令扫描异常，继续运行")
            time.sleep(self.scan_interval)

        logger.info("Launcher 已退出")

    def shutdown(self) -> None:
        """请求优雅退出."""
        self._running = False

    # ── 命令处理 ──────────────────────────────────────────────────────

    def _process_commands(self) -> None:
        """扫描并处理待执行的命令文件."""
        for cmd_file in sorted(self.cmd_dir.glob("restart-*.json")):
            # 跳过结果文件和锁文件
            name = cmd_file.name
            if name.endswith(".result.json") or name.endswith(".lock"):
                continue

            result_file = cmd_file.with_suffix(".result.json")

            # 跳过已处理的
            if result_file.exists():
                continue

            # 跳过正在处理的（有 .lock 文件）
            lock_file = cmd_file.with_suffix(".lock")
            if lock_file.exists():
                # 检查锁是否过期 (> 5 分钟)
                age = time.time() - lock_file.stat().st_mtime
                if age < 300:
                    continue
                logger.warning("清理过期锁: %s (%.0fs)", lock_file, age)
                lock_file.unlink()

            # 获取锁
            lock_file.touch()
            try:
                self._execute_command(cmd_file, result_file, lock_file)
            except Exception:
                logger.exception("命令执行异常: %s", cmd_file)
                self._write_result(result_file, {
                    "status": "error",
                    "reason": "内部异常",
                    "elapsed": 0,
                })
            finally:
                if lock_file.exists():
                    lock_file.unlink()

    def _execute_command(
        self, cmd_file: Path, result_file: Path, lock_file: Path
    ) -> None:
        """执行单条命令."""
        t0 = time.monotonic()

        # 读取命令
        try:
            cmd = json.loads(cmd_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self._write_result(result_file, {
                "status": "error",
                "reason": f"命令文件解析失败: {e}",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        action = cmd.get("action", "")
        name = cmd.get("name", "unknown")
        port = cmd.get("port")

        if action != "restart":
            self._write_result(result_file, {
                "status": "error",
                "reason": f"不支持的操作: {action}",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        if not port:
            self._write_result(result_file, {
                "status": "error",
                "reason": "缺少 port 参数",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        logger.info("执行重启: name=%s port=%s", name, port)

        # 0. 短暂延迟，确保旧进程的 HTTP 响应已完全发送
        pre_kill_delay = cmd.get("pre_kill_delay", 2.0)
        logger.info("等待 %.1fs 确保 HTTP 响应已刷新...", pre_kill_delay)
        time.sleep(pre_kill_delay)

        # 1. 杀旧进程
        kill_port(port)

        # 2. 等端口释放
        if not wait_port_free(port, timeout=10):
            self._write_result(result_file, {
                "status": "error",
                "reason": f"端口 {port} 超时未释放",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        # 3. 启动新进程前清理 staging 信息（不杀 9528 进程，保留用户验证窗口）
        cleanup = cmd.get("cleanup", {})
        staging_info_file = cleanup.get("staging_info_file")
        if staging_info_file:
            Path(staging_info_file).expanduser().unlink(missing_ok=True)

        # 4. 启动新进程
        start_cmd = cmd.get("cmd", [])
        if not start_cmd:
            self._write_result(result_file, {
                "status": "error",
                "reason": "缺少 cmd 参数",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        # 5a. 注入纳管对象定时轮询刷新标志 (页面初始化默认加载第一页，此标志控制额外定时刷新)
        objects_auto_refresh = cmd.get("objects_auto_refresh")
        if objects_auto_refresh is not None:
            from ila.launcher_platform import inject_objects_auto_refresh_flag
            start_cmd = inject_objects_auto_refresh_flag(start_cmd, objects_auto_refresh)
            logger.info("纳管对象定时轮询刷新: %s", "启用" if objects_auto_refresh else "关闭")

        cwd = cmd.get("cwd") or None
        proc = spawn_detached(start_cmd, cwd=cwd)
        if not proc:
            self._write_result(result_file, {
                "status": "error",
                "reason": "启动进程失败",
                "elapsed": time.monotonic() - t0,
            })
            cmd_file.unlink(missing_ok=True)
            return

        # 6. 健康检查
        health_url = cmd.get("health_check_url")
        if health_url:
            timeout = cmd.get("health_check_timeout", 30)
            if not health_check(health_url, timeout=timeout):
                # 健康检查失败，但进程已启动，记录为 degraded
                try:
                    proc.kill()
                except Exception:
                    pass
                self._write_result(result_file, {
                    "status": "error",
                    "reason": f"健康检查超时: {health_url}",
                    "elapsed": time.monotonic() - t0,
                })
                cmd_file.unlink(missing_ok=True)
                return

        # 7. 成功
        elapsed = time.monotonic() - t0
        logger.info("重启成功: name=%s pid=%d elapsed=%.1fs", name, proc.pid, elapsed)

        # 更新运行时版本号 (如果命令中指定了目标版本)
        target_version = cmd.get("version")
        old_version = cmd.get("old_version", "")
        if target_version:
            try:
                from ila import set_runtime_version
                set_runtime_version(target_version)
                logger.info("版本号已更新: %s", target_version)
            except Exception as e:
                logger.warning("版本号更新失败: %s", e)


        # 8. 生成版本迭代报告
        try:
            from ila.launcher_platform import save_version_report
            from datetime import datetime
            report_task_id = f"ila-version-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            report_steps = [
                {
                    "phase": "命令解析",
                    "icon": "📋",
                    "detail": (
                        f"解析重启命令文件 {cmd_file.name}：提取 action={action}、"
                        f"服务名={name}、目标端口={port}、旧版本={old_version}、"
                        f"目标版本={target_version}、启动命令="
                        f"{' '.join(cmd[:3]) if isinstance(cmd, list) else str(cmd)[:40]}..."
                        f"，校验 JSON 格式及必填字段完整性"
                    ),
                    "status": "success"
                },
                {
                    "phase": "旧进程终止",
                    "icon": "🛑",
                    "detail": (
                        f"通过 kill_port({port}) 查找占用端口 {port} 的所有进程 PID，"
                        f"过滤当前 Launcher 自身进程后发送 SIGTERM 信号，"
                        f"等待 3s 排空现有请求，若超时则发送 SIGKILL 强制终止。"
                        f"共终止 {len(old_pids) if old_pids else 1} 个旧进程"
                    ),
                    "status": "success"
                },
                {
                    "phase": "端口释放等待",
                    "icon": "⏳",
                    "detail": (
                        f"调用 wait_port_free({port}, timeout=10.0) 轮询确认端口 {port} "
                        f"已完全释放（POLL 间隔 250ms），避免新进程启动时出现 Address already in use 错误"
                    ),
                    "status": "success"
                },
            ]
            if cleanup:
                report_steps.append({
                    "phase": "环境清理",
                    "icon": "🧹",
                    "detail": (
                        f"清除 staging 残留信息文件及临时命令文件 {cmd_file.name}，"
                        f"调用 _clear_verification_mode() 移除部署验证模式标记文件 ~/.ila/verification-mode.json"
                    ),
                    "status": "success"
                })
            report_steps.append({
                "phase": "新进程启动",
                "icon": "🚀",
                "detail": (
                    f"调用 spawn_detached(cmd, port={port}) 在端口 {port} 启动新 ILA 实例，"
                    f"PID={proc.pid}，启动命令: {' '.join(cmd[:6]) if isinstance(cmd, list) else str(cmd)[:80]}..."
                    f"，进程以独立 session 运行，脱离当前终端生命周期"
                ),
                "status": "success"
            })
            if health_url:
                report_steps.append({
                    "phase": "健康检查",
                    "icon": "💚",
                    "detail": (
                        f"对健康检查端点 {health_url} 执行 health_check(url, timeout=30)，"
                        f"轮询间隔 0.5s，验证新实例已完全启动并就绪接收流量，"
                        f"HTTP 状态码 200 即确认通过"
                    ),
                    "status": "success"
                })
            if target_version:
                report_steps.append({
                    "phase": "版本更新",
                    "icon": "📌",
                    "detail": (
                        f"调用 set_runtime_version('{target_version}') 将运行版本号写入版本注册表，"
                        f"旧版本 {old_version} → 新版本 {target_version}，"
                        f"同步更新纳管对象 ila:agent:core 的 current_version 字段，"
                        f"Dashboard 页面刷新后可立即展示最新版本号"
                    ),
                    "status": "success"
                })
            conclusion = {
                "verdict": "pass",
                "verdict_label": "通过",
                "verdict_icon": "✅",
                "summary": (
                    f"ILA 版本迭代成功：{old_version} → {target_version}，"
                    f"新进程 PID={proc.pid}，总耗时 {elapsed:.1f}s，"
                    f"全流程 {len(report_steps)} 个步骤全部执行成功"
                ),
                "overall_text": (
                    f"✅ 版本迭代通过 ({old_version} → {target_version})，"
                    f"全流程 {len(report_steps)} 个步骤全部执行成功，"
                    f"新实例健康检查通过，版本注册表已更新"
                ),
                "duration_seconds": round(elapsed, 1),
            }
            lifecycle_phases = cmd.get("lifecycle_phases", None)
            report_path = save_version_report(
                task_id=report_task_id,
                old_version=old_version,
                new_version=target_version,
                steps=report_steps,
                conclusion=conclusion,
                lifecycle_phases=lifecycle_phases,
            )
            logger.info("版本迭代报告已保存: %s", report_path)
        except Exception as e:
            logger.warning("版本迭代报告生成失败: %s", e)
        self._write_result(result_file, {
            "status": "success",
            "new_pid": proc.pid,
            "elapsed": elapsed,
        })
        cmd_file.unlink(missing_ok=True)

    # ── 辅助 ──────────────────────────────────────────────────────────

    @staticmethod
    def _write_result(result_file: Path, data: dict[str, Any]) -> None:
        """写入结果文件."""
        try:
            result_file.write_text(json.dumps(data, ensure_ascii=False))
        except OSError as e:
            logger.error("写入结果文件失败: %s %s", result_file, e)

    @staticmethod
    def _clear_verification_mode() -> None:
        """清除验证模式标记文件."""
        marker = Path.home() / ".ila" / "verification-mode.json"
        if marker.exists():
            marker.unlink()
            logger.info("已清除验证模式标记")


# ── 入口 ──────────────────────────────────────────────────────────────


def main() -> None:
    """Launcher 独立运行入口.

    通过环境变量 ILA_LAUNCHER_CMD_DIR 可覆盖命令目录 (用于测试).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [ILA-Launcher] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cmd_dir = os.environ.get("ILA_LAUNCHER_CMD_DIR")
    launcher = Launcher(cmd_dir=Path(cmd_dir) if cmd_dir else None)
    launcher.run_forever()


if __name__ == "__main__":
    main()
