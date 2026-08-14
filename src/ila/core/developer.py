"""沙箱开发模块 — 在隔离环境中通过 Codex CLI 执行代码开发."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
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
        # 默认模型与 config/ila_config.yaml 保持一致 (deepseek-v4-pro)
        self.codex_model = self.config.get("codex_model", "deepseek-v4-pro")
        self.codex_sandbox_mode = self.config.get("codex_sandbox_mode", "bypass")
        self.max_retries = self.config.get("max_retries", 3)
        self.timeout = self.config.get("timeout", 600)

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
                logger.info("重置沙箱并重试...")
                self.sandbox_manager.reset_sandbox(sandbox_path, obj)
                self._inject_task_spec(sandbox_path, task_spec)

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
4. 完成后检查代码完整性（不要运行测试命令）
5. 不要修改 .git 目录
6. **【验证标记】修改 HTML 元素时必须加 data-verification-modified="true" 属性**:
   - 每次修改或新增 dashboard.html 中的 HTML 板块、控件、section 时, 在对应元素上添加 `data-verification-modified="true"` 属性
   - 例: `<div class="section" data-module="dashboard" data-verification-modified="true">`
   - 不要删除已有的 data-verification-modified 属性
   - 这个属性用于部署验证阶段自动高亮新修改的内容

## 当前版本
{task_spec.current_version}
"""
        agents_path = os.path.join(sandbox_path, "AGENTS.md")
        with open(agents_path, "w", encoding="utf-8") as f:
            f.write(agents_md)
        logger.info("任务规格已注入: %s", agents_path)

    def _develop_with_codex(self, sandbox_path: str, task_spec: TaskSpec) -> dict[str, Any]:
        """通过 Codex CLI 执行开发."""
        prompt = self._build_prompt(task_spec)

        # 构建命令 (Windows 上 codex 可能是 .exe 或 .CMD)
        codex_cmd = shutil.which("codex") or "codex"
        if not shutil.which("codex"):
            return {
                "status": "error",
                "reason": "Codex CLI 未安装。请配置开发框架:\n"
                          "  安装: npm install -g @openai/codex  或  brew install codex\n"
                          "  或改配置: config/ila_config.yaml → sandbox.framework: claude_code / hermes_delegate\n"
                          "  并确认: codex exec --version",
            }
        cmd = [
            codex_cmd, "exec",
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
                stdin=subprocess.DEVNULL,  # 防止 codex 等待 stdin 输入
            )

            if result.returncode == 0:
                output = result.stdout
                # 检查是否有明显的错误标记
                if "error" in output.lower() and "succeeded" not in output.lower():
                    logger.warning("Codex 输出包含 error，检查结果...")
                return {"status": "success", "output": output[-2000:]}
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
        """通过 Claude Code CLI 执行开发.

        Windows 注意: npm 安装的 claude 是 claude.CMD, subprocess 不带
        shell=True 时不会执行 .CMD 文件, 会误报 FileNotFoundError.
        """
        prompt = self._build_prompt(task_spec)

        # 首次调用前握手: 验证 CLI 可用性 + API/订阅连通性 (claude --print "ping"),
        # 失败时降级重试一次, 避免开发重试 3 次才发现环境问题
        handshake_ok, handshake_err = self._claude_handshake()
        if not handshake_ok:
            return {
                "status": "error",
                "reason": f"Claude Code CLI 不可用: {handshake_err}\n"
                          "  安装: npm install -g @anthropic-ai/claude-code\n"
                          "  登录: claude  (首次运行 OAuth 登录, 或设置 ANTHROPIC_API_KEY)\n"
                          "  或改配置: config/ila_config.yaml → sandbox.framework: codex / hermes_delegate",
            }

        claude_cmd = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [claude_cmd, "--print", prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=sandbox_path,
                shell=(sys.platform == "win32"),
            )
            if result.returncode == 0:
                return {"status": "success", "output": result.stdout[-2000:]}
            # F1: claude 的错误 (API Error / 订阅错误) 通常输出在 stdout, stderr 为空,
            # 因此拼接两个通道取末尾, 避免错误原因永远为空
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            return {
                "status": "error",
                "reason": f"Claude Code 退出码 {result.returncode}: {combined[-500:]}",
                "output": result.stdout[-1000:],
            }
        except FileNotFoundError:
            return {"status": "error", "reason": "Claude Code CLI 未安装"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": f"Claude Code 超时 ({self.timeout}s)"}

    def _claude_handshake(self) -> tuple[bool, str]:
        """握手检查 claude CLI 可用性 + API/订阅连通性.

        使用 ``claude --print "ping"`` (5s 超时) 同时覆盖 CLI 本地可用性
        与后端 API 连通性; 失败时降级重试一次 (Windows + shell=True 的
        cmd 包装开销可能导致偶发超时).

        Returns:
            (是否可用, 错误详情)
        """
        claude_cmd = shutil.which("claude") or "claude"
        last_err = ""
        for attempt in range(2):
            try:
                result = subprocess.run(
                    [claude_cmd, "--print", "ping"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    shell=(sys.platform == "win32"),
                )
                if result.returncode == 0:
                    return True, ""
                combined = (result.stdout or "") + "\n" + (result.stderr or "")
                last_err = combined.strip()[-300:] or f"退出码 {result.returncode}"
            except FileNotFoundError:
                return False, "CLI 未安装"
            except subprocess.TimeoutExpired:
                last_err = f"握手超时 (5s, 尝试 {attempt + 1}/2)"
        return False, last_err

    def _build_prompt(self, task_spec: TaskSpec) -> str:
        """构建开发框架的提示词."""
        changes = "\n".join(
            f"  {i+1}. {c.description}"
            for i, c in enumerate(task_spec.changes)
        )
        files = ", ".join(task_spec.changes[0].files) if task_spec.changes else ""
        return f"""请修改当前目录中的文件以完成以下需求。请高效操作，不要运行测试。

需求: {task_spec.requirement}

涉及文件: {files}

要求:
1. 直接用工具修改文件，不要只描述计划
2. 只修改必要的文件
3. 遵循现有代码风格
4. 不要运行 pytest 或其他测试命令
5. **【验证标记】修改 dashboard.html 中的 HTML 元素时，必须给被修改的元素添加 data-verification-modified="true" 属性，用于部署验证阶段自动高亮新修改的内容。例: <div class="section" data-module="dashboard" data-verification-modified="true"> 不要删除已有的该属性。"""

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
