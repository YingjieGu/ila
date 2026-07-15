"""沙箱开发模块 — 在隔离环境中通过 Codex CLI 执行代码开发."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject
from ila.models.task_spec import TaskSpec

logger = logging.getLogger(__name__)


class Developer:
    """沙箱开发模块.

    在隔离沙箱中调用 Codex CLI (或其他开发框架) 执行代码开发。
    流程: 创建沙箱 → 复制目标对象 → 注入任务规格 → 调用 Codex → 沙箱内测试
    """

    def __init__(self, adapter: PlatformAdapter, sandbox_manager: Any,
                 config: dict | None = None):
        """初始化开发模块.

        Args:
            adapter: 平台适配器
            sandbox_manager: 沙箱管理器实例
            config: 开发配置:
                - framework: 'codex' | 'claude_code' | 'hermes_delegate' (默认 'codex')
                - codex_model: Codex 使用的模型 (默认 'glm-5.2')
                - codex_sandbox_mode: 'bypass' | 'workspace_write' | 'danger_full_access'
                - max_retries: 最大重试次数 (默认 3)
                - timeout: 超时秒数 (默认 300)
        """
        self.adapter = adapter
        self.sandbox_manager = sandbox_manager
        self.config = config or {}
        self.framework = self.config.get("framework", "codex")
        self.codex_model = self.config.get("codex_model", "glm-5.2")
        self.codex_sandbox_mode = self.config.get("codex_sandbox_mode", "bypass")
        self.max_retries = self.config.get("max_retries", 3)
        self.timeout = self.config.get("timeout", 300)

    def develop(self, obj: ManagedObject, task_spec: TaskSpec) -> dict[str, Any]:
        """在沙箱中开发新版本.

        Args:
            obj: 目标对象
            task_spec: 任务规格书

        Returns:
            开发结果:
            - ``{"status": "success", "sandbox_path": "...", "changed_files": [...]}``
            - ``{"status": "failed", "reason": "...", "sandbox_path": "..."}``
        """
        sandbox_level = task_spec.sandbox_level or "tempdir"

        # 1. 创建沙箱
        logger.info("创建沙箱 (level=%s): %s", sandbox_level, obj.name)
        sandbox_path = self.sandbox_manager.create_sandbox(obj, level=sandbox_level)

        # 2. 注入任务规格 (创建 AGENTS.md 引导文件)
        self._inject_task_spec(sandbox_path, task_spec)

        # 3. 调用开发框架
        for attempt in range(1, self.max_retries + 1):
            logger.info("调用 %s 开发 (尝试 %d/%d)...", self.framework, attempt, self.max_retries)

            if self.framework == "codex":
                result = self._develop_with_codex(sandbox_path, task_spec)
            elif self.framework == "claude_code":
                result = self._develop_with_claude_code(sandbox_path, task_spec)
            else:
                result = {"status": "error", "reason": f"未知框架: {self.framework}"}

            if result.get("status") == "success":
                # 4. 收集变更文件
                changed_files = self._collect_changed_files(sandbox_path, obj)
                return {
                    "status": "success",
                    "sandbox_path": sandbox_path,
                    "changed_files": changed_files,
                    "dev_log": result.get("output", ""),
                }

            logger.warning("开发尝试 %d 失败: %s", attempt, result.get("reason", ""))
            if attempt < self.max_retries:
                logger.info("重试中...")

        return {
            "status": "failed",
            "reason": f"开发失败，已重试 {self.max_retries} 次",
            "sandbox_path": sandbox_path,
            "last_error": result.get("reason", ""),
        }

    def _inject_task_spec(self, sandbox_path: str, task_spec: TaskSpec) -> None:
        """注入任务规格到沙箱 (创建 AGENTS.md 引导文件)."""
        changes_text = "\n".join(
            f"- [{c.change_type}] {c.description} (文件: {', '.join(c.files)})"
            for c in task_spec.changes
        )
        test_text = "\n".join(
            f"- 功能: {req}" for req in task_spec.test_requirements.functional
        )

        agents_md = f"""# ILA 开发任务

## 任务描述
{task_spec.requirement}

## 变更清单
{changes_text}

## 测试要求
{test_text}

## 约束
1. 只修改任务相关的文件，不碰触其他文件
2. 遵循现有代码风格
3. 为新增功能编写测试
4. 完成后运行 pytest 验证
5. 不要修改 .git 目录

## 当前版本
{task_spec.current_version}
"""
        agents_path = os.path.join(sandbox_path, "AGENTS.md")
        with open(agents_path, "w") as f:
            f.write(agents_md)
        logger.info("任务规格已注入: %s", agents_path)

    def _develop_with_codex(self, sandbox_path: str, task_spec: TaskSpec) -> dict[str, Any]:
        """通过 Codex CLI 执行开发."""
        prompt = self._build_prompt(task_spec)

        # 构建命令
        cmd = [
            "codex", "exec",
            "-m", self.codex_model,
        ]

        # 沙箱模式
        if self.codex_sandbox_mode == "bypass":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.codex_sandbox_mode == "workspace_write":
            cmd.extend(["-s", "workspace-write"])
        elif self.codex_sandbox_mode == "danger_full_access":
            cmd.extend(["-s", "danger-full-access"])

        cmd.extend([
            "--skip-git-repo-check",
            "-C", sandbox_path,
            prompt,
        ])

        logger.info("Codex 命令: %s", " ".join(cmd[:4]) + " ...")
        logger.debug("完整命令: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=sandbox_path,
            )

            if result.returncode == 0:
                output = result.stdout
                # 检查是否有明显的错误标记
                if "error" in output.lower() and "succeeded" not in output.lower():
                    # 有 error 但没有 succeeded，可能部分失败
                    logger.warning("Codex 输出包含 error，检查结果...")
                return {"status": "success", "output": output[-2000:]}  # 截取最后2000字符
            else:
                return {
                    "status": "error",
                    "reason": f"Codex 退出码 {result.returncode}: {result.stderr[:500]}",
                    "output": result.stdout[-1000:],
                }
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": f"Codex 超时 ({self.timeout}s)"}
        except FileNotFoundError:
            return {"status": "error", "reason": "Codex CLI 未安装"}
        except Exception as e:
            return {"status": "error", "reason": f"调用 Codex 异常: {e}"}

    def _develop_with_claude_code(self, sandbox_path: str, task_spec: TaskSpec) -> dict[str, Any]:
        """通过 Claude Code CLI 执行开发."""
        prompt = self._build_prompt(task_spec)
        try:
            result = subprocess.run(
                ["claude", "--print", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=sandbox_path,
            )
            if result.returncode == 0:
                return {"status": "success", "output": result.stdout[-2000:]}
            return {"status": "error", "reason": f"Claude Code 退出码 {result.returncode}"}
        except FileNotFoundError:
            return {"status": "error", "reason": "Claude Code CLI 未安装"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": f"Claude Code 超时 ({self.timeout}s)"}

    def _build_prompt(self, task_spec: TaskSpec) -> str:
        """构建开发框架的提示词."""
        changes = "\n".join(
            f"  {i+1}. [{c.change_type}] {c.description}"
            for i, c in enumerate(task_spec.changes)
        )
        return f"""你是 ILA (Iteration Loop Agent) 的开发执行器。

## 任务
{task_spec.requirement}

## 具体变更
{changes}

## 约束
1. 只修改相关文件，不碰触其他文件
2. 遵循现有代码风格
3. 为新增功能编写测试
4. 完成后运行 pytest 验证 (如果有的话)

## 重要
请直接修改文件，不要只描述要做什么。完成后确认变更已完成。"""

    def _collect_changed_files(self, sandbox_path: str, obj: ManagedObject) -> list[str]:
        """收集沙箱中的变更文件列表."""
        changed = []
        for root, _dirs, files in os.walk(sandbox_path):
            # 跳过 .git 和 __pycache__
            if ".git" in root or "__pycache__" in root:
                continue
            for fname in files:
                if fname in ("AGENTS.md",):  # 跳过 ILA 注入的文件
                    continue
                fpath = os.path.join(root, fname)
                changed.append(os.path.relpath(fpath, sandbox_path))
        return sorted(changed)
