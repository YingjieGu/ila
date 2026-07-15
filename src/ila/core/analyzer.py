"""需求分析模块 — 解析需求，识别目标对象，生成任务规格书."""

from __future__ import annotations

import logging
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.adapters.registry import AdapterRegistry
from ila.models.managed_object import ManagedObject
from ila.models.task_spec import ChangeItem, TaskSpec, TestRequirements

logger = logging.getLogger(__name__)


class Analyzer:
    """需求分析模块.

    解析用户的自然语言需求，识别目标对象和变更类型，
    生成结构化的任务规格书 (TaskSpec)。
    """

    def __init__(self, adapter: PlatformAdapter):
        self.adapter = adapter

    def analyze(self, obj: ManagedObject, requirement: str) -> TaskSpec:
        """分析需求，生成任务规格书.

        Args:
            obj: 目标对象
            requirement: 自然语言需求描述

        Returns:
            TaskSpec 任务规格书
        """
        task_id = TaskSpec.generate_task_id()

        # 识别变更类型
        changes = self._identify_changes(requirement, obj)

        # 推导测试需求
        test_req = self._derive_test_requirements(requirement, obj)

        # 选择沙箱级别
        sandbox_level = self._select_sandbox_level(obj)

        # 回滚计划
        rollback_plan = f"保留 {obj.path} 的 tar.gz 快照"

        spec = TaskSpec(
            task_id=task_id,
            target_object_id=obj.object_id,
            target_platform=obj.platform,
            target_path=obj.path,
            current_version=obj.current_version,
            requirement=requirement,
            changes=changes,
            test_requirements=test_req,
            sandbox_level=sandbox_level,
            rollback_plan=rollback_plan,
        )

        logger.info("任务规格书已生成: %s -> %s", obj.object_id, task_id)
        return spec

    def _identify_changes(self, requirement: str, obj: ManagedObject) -> list[ChangeItem]:
        """识别需要的代码变更.

        使用关键词匹配 + 对象文件列表推断变更类型和涉及文件。
        """
        req_lower = requirement.lower()

        # 判断变更类型
        change_type = "feature"
        if any(kw in req_lower for kw in ["修复", "fix", "bug", "错误", "crash", "异常"]):
            change_type = "bugfix"
        elif any(kw in req_lower for kw in ["重构", "refactor", "优化结构"]):
            change_type = "refactor"
        elif any(kw in req_lower for kw in ["优化", "optimize", "性能", "加速"]):
            change_type = "optimization"
        elif any(kw in req_lower for kw in ["添加", "新增", "add", "新功能", "支持"]):
            change_type = "feature"

        # 推断涉及的文件
        files = self.adapter.get_object_files(obj)
        relevant_files = [f for f in files if not f.endswith((".gitignore",))]
        # 优先选择代码文件
        code_files = [f for f in relevant_files
                      if f.endswith((".py", ".js", ".ts", ".sh", ".md"))]
        if not code_files:
            code_files = relevant_files[:3] if relevant_files else []

        return [ChangeItem(
            change_type=change_type,
            description=requirement[:200],  # 截取前200字符
            files=[f.split("/")[-1] for f in code_files],
            estimated_complexity=self._estimate_complexity(requirement),
        )]

    def _derive_test_requirements(self, requirement: str,
                                   obj: ManagedObject) -> TestRequirements:
        """推导测试需求."""
        req_lower = requirement.lower()

        functional = ["对象能正常加载和调用"]
        regression = ["原有功能不受影响"]
        performance: list[str] = []
        security: list[str] = []
        compatibility = ["与平台其他对象兼容"]

        # 根据需求内容补充
        if any(kw in req_lower for kw in ["性能", "optimize", "加速"]):
            performance.append("响应时间不超过旧版本 1.2x")

        if any(kw in req_lower for kw in ["安全", "security", "注入", "权限"]):
            security.append("无新增安全风险")

        if any(kw in req_lower for kw in ["修复", "fix", "bug"]):
            functional.append(f"修复的问题不再复现: {requirement[:100]}")

        return TestRequirements(
            functional=functional,
            regression=regression,
            performance=performance,
            security=security,
            compatibility=compatibility,
        )

    def _select_sandbox_level(self, obj: ManagedObject) -> str:
        """根据对象类型选择沙箱级别."""
        if obj.object_type in ("skill", "config"):
            return "tempdir"
        elif obj.object_type in ("plugin", "tool"):
            return "tempdir"
        elif obj.object_type in ("mcp", "agent"):
            return "tempdir"
        return "tempdir"

    def _estimate_complexity(self, requirement: str) -> str:
        """估算变更复杂度."""
        # 简单启发式：根据描述长度
        if len(requirement) < 50:
            return "low"
        elif len(requirement) < 200:
            return "medium"
        else:
            return "high"

    @staticmethod
    def find_target_object(object_id: str) -> ManagedObject | None:
        """通过适配器注册表查找目标对象.

        Args:
            object_id: 对象 ID (e.g. ``hermes:skill:my-skill``)

        Returns:
            ManagedObject 或 None
        """
        return AdapterRegistry.find_object(object_id)
