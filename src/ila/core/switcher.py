"""热切换编排与回滚模块."""

from __future__ import annotations

import logging
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


class Switcher:
    """热切换编排模块.

    负责编排热切换流程：前置检查 → 调用适配器 hot_swap → 处理结果 → 自动回滚。
    """

    def __init__(self, adapter: PlatformAdapter, auto_rollback: bool = True):
        self.adapter = adapter
        self.auto_rollback = auto_rollback

    def switch(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """执行热切换.

        Args:
            obj: 目标对象
            sandbox_path: 沙箱工作区路径

        Returns:
            结果字典:
            - success: ``{"status": "success", "snapshot": "..."}``
            - rolled_back: ``{"status": "rolled_back", "reason": "...", "snapshot": "..."}``
            - error: ``{"status": "error", "reason": "..."}``
        """
        # 前置检查
        files = self.adapter.get_object_files(obj)
        if not files and obj.object_type not in ("mcp", "config"):
            logger.warning("对象 %s 没有文件", obj.object_id)

        # 调用适配器的热切换
        # 适配器内部处理: 快照 → 原子替换 → 重载 → 健康检查 → 自动回滚
        result = self.adapter.hot_swap(obj, sandbox_path)

        if result["status"] == "success":
            logger.info("热切换成功: %s", obj.object_id)
        elif result["status"] == "rolled_back":
            logger.warning("热切换已回滚: %s (原因: %s)", obj.object_id, result.get("reason"))
        elif result["status"] == "error" and self.auto_rollback:
            # 适配器没有自动回滚，手动回滚
            snapshot = result.get("snapshot")
            if snapshot:
                logger.info("尝试手动回滚...")
                roller = Roller(self.adapter)
                rollback_result = roller.rollback(obj, snapshot)
                result["rollback"] = rollback_result

        return result


class Roller:
    """回滚编排模块.

    负责从快照恢复对象到之前的版本。
    """

    def __init__(self, adapter: PlatformAdapter):
        self.adapter = adapter

    def rollback(self, obj: ManagedObject, snapshot_path: str) -> dict[str, Any]:
        """从快照回滚.

        Args:
            obj: 目标对象
            snapshot_path: 快照文件路径

        Returns:
            ``{"status": "success"|"failed"|"restored_but_unhealthy", "reason": "..."}``
        """
        logger.info("开始回滚: %s <- %s", obj.object_id, snapshot_path)

        success = self.adapter.restore_snapshot(obj, snapshot_path)
        if not success:
            return {"status": "failed", "reason": "快照恢复失败"}

        # 重载
        reloaded = self.adapter.reload(obj)
        if not reloaded:
            logger.warning("回滚后重载失败")

        # 健康检查
        healthy = self.adapter.health_check(obj)
        if healthy:
            logger.info("回滚成功: %s", obj.object_id)
            return {"status": "success"}
        else:
            logger.warning("回滚后健康检查失败")
            return {"status": "restored_but_unhealthy", "reason": "文件已恢复但健康检查未通过"}

    def rollback_to_version(self, obj: ManagedObject, snapshot_path: str) -> dict[str, Any]:
        """回滚到指定版本的快照 (等同于 rollback)."""
        return self.rollback(obj, snapshot_path)
