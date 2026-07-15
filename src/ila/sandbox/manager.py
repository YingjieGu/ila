"""SandboxManager: 为 ILA 迭代开发创建隔离工作区.

支持三种隔离级别:
- tempdir (默认): 复制目标对象到临时目录
- worktree: git worktree (轻量，需要 git 仓库)
- docker: Docker 容器沙箱 (可选骨架)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Any

from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


class SandboxManager:
    """沙箱管理器 - 为迭代开发创建隔离工作区.

    每个沙箱在创建时分配唯一 sandbox_id，并记录创建时间、关联对象等元信息。
    所有元信息存储在沙箱目录下的 ``.ila-sandbox.json`` 文件中。

    Args:
        workspace_root: 沙箱根目录 (默认 ``/tmp``)
    """

    META_FILENAME = ".ila-sandbox.json"

    def __init__(self, workspace_root: str = "/tmp") -> None:
        self.workspace_root = workspace_root
        os.makedirs(self.workspace_root, exist_ok=True)

    def create_sandbox(self, obj: ManagedObject, level: str = "tempdir") -> str:
        """创建沙箱工作区.

        Args:
            obj: 目标纳管对象
            level: 隔离级别 (``tempdir`` | ``worktree`` | ``docker``)

        Returns:
            沙箱路径

        Raises:
            ValueError: 未知的隔离级别
            FileNotFoundError: 目标对象路径不存在
        """
        if level not in ("tempdir", "worktree", "docker"):
            raise ValueError(
                f"未知的沙箱级别: {level!r}，支持: tempdir, worktree, docker"
            )

        sandbox_id = self._generate_sandbox_id()
        logger.info("创建沙箱 [%s] 级别=%s 对象=%s", sandbox_id, level, obj.object_id)

        if level == "tempdir":
            sandbox_path = self._create_tempdir_sandbox(obj, sandbox_id)
        elif level == "worktree":
            sandbox_path = self._create_worktree_sandbox(obj, sandbox_id)
        elif level == "docker":
            sandbox_path = self._create_docker_sandbox(obj, sandbox_id)

        self._write_meta(sandbox_path, {
            "sandbox_id": sandbox_id,
            "level": level,
            "object_id": obj.object_id,
            "object_name": obj.name,
            "object_path": obj.path,
            "created_at": datetime.now().isoformat(),
            "workspace_root": self.workspace_root,
        })
        logger.info("沙箱创建完成: %s", sandbox_path)
        return sandbox_path

    def cleanup(self, sandbox_path: str) -> bool:
        """清理沙箱工作区.

        Args:
            sandbox_path: 沙箱路径

        Returns:
            是否清理成功
        """
        if not os.path.exists(sandbox_path):
            logger.warning("沙箱路径不存在: %s", sandbox_path)
            return False

        meta = self.get_sandbox_info(sandbox_path)
        level = meta.get("level", "tempdir")

        try:
            if level == "worktree":
                self._cleanup_worktree(sandbox_path)
            else:
                shutil.rmtree(sandbox_path)
            logger.info("沙箱已清理: %s", sandbox_path)
            return True
        except Exception as e:
            logger.error("清理沙箱失败: %s - %s", sandbox_path, e)
            return False

    def get_sandbox_info(self, sandbox_path: str) -> dict[str, Any]:
        """获取沙箱元信息.

        Args:
            sandbox_path: 沙箱路径

        Returns:
            元信息字典 (sandbox_id, level, object_id, created_at 等)；
            如果元信息文件不存在，返回仅含 path 的字典。
        """
        meta_path = os.path.join(sandbox_path, self.META_FILENAME)
        if not os.path.exists(meta_path):
            return {"path": sandbox_path, "exists": os.path.exists(sandbox_path)}
        import json
        with open(meta_path) as f:
            return json.load(f)

    # ---- tempdir 模式 ----

    def _create_tempdir_sandbox(self, obj: ManagedObject, sandbox_id: str) -> str:
        """tempdir 模式: 复制目标对象到临时目录."""
        if not os.path.exists(obj.path):
            raise FileNotFoundError(f"目标对象路径不存在: {obj.path}")

        sandbox_path = os.path.join(self.workspace_root, sandbox_id)
        os.makedirs(sandbox_path, exist_ok=True)

        dest = os.path.join(sandbox_path, obj.name)
        shutil.copytree(obj.path, dest)
        logger.debug("tempdir 沙箱: %s -> %s", obj.path, dest)
        return sandbox_path

    # ---- worktree 模式 ----

    def _create_worktree_sandbox(self, obj: ManagedObject, sandbox_id: str) -> str:
        """worktree 模式: 用 git worktree add 创建隔离工作区.

        如果目标路径不在 git 仓库中，自动降级到 tempdir 模式。
        """
        if not os.path.exists(obj.path):
            raise FileNotFoundError(f"目标对象路径不存在: {obj.path}")

        if not self._is_git_repo(obj.path):
            logger.warning(
                "对象 %s 不在 git 仓库中，worktree 降级为 tempdir", obj.object_id
            )
            return self._create_tempdir_sandbox(obj, sandbox_id)

        sandbox_path = os.path.join(self.workspace_root, sandbox_id)
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", sandbox_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            # 将对象文件复制到 worktree 中
            dest = os.path.join(sandbox_path, obj.name)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(obj.path, dest)
            logger.debug("worktree 沙箱: %s -> %s", obj.path, dest)
            return sandbox_path
        except subprocess.CalledProcessError as e:
            logger.warning(
                "git worktree add 失败 (rc=%d): %s，降级为 tempdir",
                e.returncode, e.stderr.strip(),
            )
            return self._create_tempdir_sandbox(obj, sandbox_id)

    def _cleanup_worktree(self, sandbox_path: str) -> None:
        """清理 worktree: 先 git worktree remove，再删除目录."""
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", sandbox_path],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except subprocess.CalledProcessError:
            # worktree remove 失败时回退到直接删除目录
            pass
        if os.path.exists(sandbox_path):
            shutil.rmtree(sandbox_path)

    # ---- docker 模式 ----

    def _create_docker_sandbox(self, obj: ManagedObject, sandbox_id: str) -> str:
        """docker 模式: Docker 容器沙箱 (骨架，尚未实现)."""
        raise NotImplementedError("Docker sandbox not yet implemented")

    # ---- 辅助方法 ----

    def _generate_sandbox_id(self) -> str:
        """生成唯一沙箱 ID: ila-sandbox-<timestamp>-<short-uuid>."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"ila-sandbox-{timestamp}-{short_uuid}"

    def _is_git_repo(self, path: str) -> bool:
        """检查路径是否在 git 仓库中."""
        try:
            result = subprocess.run(
                ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def _write_meta(self, sandbox_path: str, meta: dict[str, Any]) -> None:
        """将沙箱元信息写入沙箱目录."""
        import json
        meta_path = os.path.join(sandbox_path, self.META_FILENAME)
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
