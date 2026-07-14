"""ManagedObject: 被纳管的能力对象的统一表示."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ManagedObject:
    """被纳管的能力对象。

    所有平台（Hermes、OpenClaw 等）的能力对象统一表示为 ManagedObject。
    object_id 格式: ``platform:type:name`` (e.g. ``hermes:skill:my-skill``)

    Attributes:
        object_id: 全局唯一标识，格式 ``platform:type:name``
        platform: 平台标识 (``hermes``, ``openclaw``, ``custom``)
        object_type: 对象类型 (``skill``, ``plugin``, ``mcp``, ``agent``, ``tool``, ``config``)
        name: 对象名称
        path: 文件系统路径
        current_version: 当前版本号
        metadata: 平台特定的元数据
    """

    object_id: str
    platform: str
    object_type: str
    name: str
    path: str
    current_version: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验 object_id 格式一致性."""
        parts = self.object_id.split(":")
        if len(parts) < 3:
            raise ValueError(
                f"object_id 必须格式为 'platform:type:name', 得到: {self.object_id!r}"
            )
        # 如果 platform / object_type 没有显式设置，从 object_id 推导
        if not self.platform:
            self.platform = parts[0]
        if not self.object_type:
            self.object_type = parts[1]

    @classmethod
    def make_id(cls, platform: str, object_type: str, name: str) -> str:
        """构造标准 object_id."""
        return f"{platform}:{object_type}:{name}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典."""
        return {
            "object_id": self.object_id,
            "platform": self.platform,
            "object_type": self.object_type,
            "name": self.name,
            "path": self.path,
            "current_version": self.current_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManagedObject:
        """从字典反序列化."""
        return cls(
            object_id=data["object_id"],
            platform=data.get("platform", ""),
            object_type=data.get("object_type", ""),
            name=data["name"],
            path=data["path"],
            current_version=data.get("current_version", "unknown"),
            metadata=data.get("metadata", {}),
        )
