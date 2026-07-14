"""适配器注册表 — 管理所有已注册的平台适配器."""

from __future__ import annotations

import logging
from typing import Any

from ila.models.managed_object import ManagedObject
from ila.adapters.base import PlatformAdapter

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """平台适配器注册表.

    管理所有已注册的 PlatformAdapter 实例，提供按平台 ID 查找的能力。
    支持跨平台对象发现。
    """

    _adapters: dict[str, PlatformAdapter] = {}

    @classmethod
    def register(cls, adapter: PlatformAdapter) -> None:
        """注册平台适配器.

        Args:
            adapter: PlatformAdapter 实例
        """
        platform_id = adapter.platform_id()
        if platform_id in cls._adapters:
            logger.warning("覆盖已注册的适配器: %s", platform_id)
        cls._adapters[platform_id] = adapter
        logger.info("已注册平台适配器: %s", platform_id)

    @classmethod
    def unregister(cls, platform_id: str) -> bool:
        """取消注册平台适配器.

        Returns:
            是否成功取消（不存在则返回 False）
        """
        if platform_id in cls._adapters:
            del cls._adapters[platform_id]
            logger.info("已取消注册平台适配器: %s", platform_id)
            return True
        return False

    @classmethod
    def get_adapter(cls, platform_id: str) -> PlatformAdapter:
        """获取指定平台的适配器.

        Raises:
            ValueError: 平台适配器未注册
        """
        if platform_id not in cls._adapters:
            available = list(cls._adapters.keys())
            raise ValueError(
                f"未注册的平台适配器: {platform_id!r}. 已注册: {available}"
            )
        return cls._adapters[platform_id]

    @classmethod
    def get_all_adapters(cls) -> dict[str, PlatformAdapter]:
        """获取所有已注册的适配器（浅拷贝）."""
        return cls._adapters.copy()

    @classmethod
    def get_registered_platforms(cls) -> list[str]:
        """获取所有已注册的平台 ID 列表."""
        return list(cls._adapters.keys())

    @classmethod
    def discover_all_objects(cls) -> list[ManagedObject]:
        """跨平台发现所有纳管对象.

        对每个已注册的适配器调用 discover_objects()，
        某个适配器失败不影响其他适配器。
        """
        all_objects: list[ManagedObject] = []
        for platform_id, adapter in cls._adapters.items():
            try:
                objects = adapter.discover_objects()
                all_objects.extend(objects)
                logger.info("平台 %s 发现 %d 个对象", platform_id, len(objects))
            except Exception as e:
                logger.warning("适配器 %s 发现对象失败: %s", platform_id, e)
        return all_objects

    @classmethod
    def discover_objects_by_platform(cls, platform_id: str) -> list[ManagedObject]:
        """发现指定平台的所有纳管对象."""
        adapter = cls.get_adapter(platform_id)
        return adapter.discover_objects()

    @classmethod
    def find_object(cls, object_id: str) -> ManagedObject | None:
        """跨平台查找指定对象.

        Args:
            object_id: 对象 ID (e.g. ``hermes:skill:my-skill``)

        Returns:
            ManagedObject 或 None
        """
        parts = object_id.split(":", 2)
        if len(parts) < 2:
            return None
        platform = parts[0]
        if platform not in cls._adapters:
            return None
        adapter = cls._adapters[platform]
        try:
            return adapter.get_object(object_id)
        except Exception as e:
            logger.warning("查找对象 %s 失败: %s", object_id, e)
            return None

    @classmethod
    def clear(cls) -> None:
        """清空所有已注册的适配器（主要用于测试）."""
        cls._adapters.clear()

    @classmethod
    def is_registered(cls, platform_id: str) -> bool:
        """检查平台是否已注册."""
        return platform_id in cls._adapters
