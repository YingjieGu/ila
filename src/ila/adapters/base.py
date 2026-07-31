"""Platform Adapter 抽象基类 — ILA 与具体平台的桥梁."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ila.models.managed_object import ManagedObject


class PlatformAdapter(ABC):
    """平台适配器抽象基类.

    每个能力纳管平台（Hermes、OpenClaw 等）实现此接口，
    ILA 核心引擎通过此接口操作任意平台，不直接依赖平台 API。
    """

    @abstractmethod
    def platform_id(self) -> str:
        """返回平台标识 (e.g. ``hermes``, ``openclaw``)."""

    @abstractmethod
    def discover_objects(self) -> list[ManagedObject]:
        """发现该平台纳管的所有能力对象."""

    @abstractmethod
    def get_object(self, object_id: str) -> ManagedObject | None:
        """获取指定对象的当前状态.

        Args:
            object_id: 对象 ID (e.g. ``hermes:skill:my-skill``)

        Returns:
            ManagedObject 或 None（不存在时）
        """

    @abstractmethod
    def create_snapshot(self, obj: ManagedObject) -> str:
        """创建对象当前版本的快照.

        Args:
            obj: 目标对象

        Returns:
            快照文件路径
        """

    @abstractmethod
    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        """从快照恢复对象.

        Args:
            obj: 目标对象
            snapshot_path: 快照文件路径

        Returns:
            是否恢复成功
        """

    @abstractmethod
    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str | dict:
        """将沙箱中的新版本部署到准生产/测试环境.

        Args:
            obj: 目标对象
            sandbox_path: 沙箱工作区路径

        Returns:
            staging 实例标识 (staging_id)，或包含 staging_url 的 dict
            (对于含 Web 页面的对象)
        """

    @abstractmethod
    def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict[str, Any]:
        """调用线上对象执行测试输入.

        Args:
            obj: 目标对象
            test_input: 测试输入 (e.g. ``{"prompt": "hello"}``)

        Returns:
            结果字典 (e.g. ``{"output": "...", "exit_code": 0}``)
        """

    @abstractmethod
    def invoke_staging(self, staging_id: str, test_input: dict) -> dict[str, Any]:
        """调用准生产环境中的对象执行测试输入.

        Args:
            staging_id: deploy_to_staging 返回的标识
            test_input: 测试输入

        Returns:
            结果字典
        """

    @abstractmethod
    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """执行热切换：将沙箱新版本原子替换到线上.

        适配器内部应处理: 快照创建 → 原子替换 → 重载 → 健康检查 → 自动回滚.

        Args:
            obj: 目标对象
            sandbox_path: 沙箱工作区路径

        Returns:
            结果字典:
            - ``{"status": "success", "snapshot": "<path>"}``
            - ``{"status": "rolled_back", "reason": "...", "snapshot": "<path>"}``
            - ``{"status": "error", "reason": "..."}``
        """

    @abstractmethod
    def health_check(self, obj: ManagedObject) -> bool:
        """健康检查：对象是否正常运行."""

    @abstractmethod
    def reload(self, obj: ManagedObject) -> bool:
        """触发平台重载该对象."""

    @abstractmethod
    def get_object_files(self, obj: ManagedObject) -> list[str]:
        """获取对象包含的所有文件列表."""

    @abstractmethod
    def validate_compatibility(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """验证新版本与平台其他对象的兼容性.

        Returns:
            ``{"compatible": bool, "issues": [str], "warnings": [str]}``
        """

    # ---- 非抽象辅助方法 ----

    def cleanup_staging(self, staging_id: str) -> None:
        """清理 staging 环境（可选实现）."""
        pass

    def get_platform_home(self) -> str:
        """返回平台主目录路径（可选实现）."""
        return ""
