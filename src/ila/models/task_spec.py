"""TaskSpec: 迭代任务规格书."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ChangeItem:
    """单个变更项."""

    change_type: str  # 'bugfix' | 'feature' | 'refactor' | 'optimization'
    description: str
    files: list[str] = field(default_factory=list)
    estimated_complexity: str = "medium"  # 'low' | 'medium' | 'high'

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "description": self.description,
            "files": self.files,
            "estimated_complexity": self.estimated_complexity,
        }


@dataclass
class TestRequirements:
    """测试需求."""

    functional: list[str] = field(default_factory=list)
    regression: list[str] = field(default_factory=list)
    performance: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    compatibility: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "functional": self.functional,
            "regression": self.regression,
            "performance": self.performance,
            "security": self.security,
            "compatibility": self.compatibility,
        }


@dataclass
class TaskSpec:
    """迭代任务规格书 — 需求分析阶段的输出.

    描述要对哪个对象做什么变更、怎么测试、用什么沙箱级别。

    Attributes:
        task_id: 唯一任务 ID (e.g. ``ila-20260714-001``)
        target_object_id: 目标对象 ID
        target_platform: 目标平台
        target_path: 目标对象路径
        current_version: 当前版本号
        requirement: 原始需求描述
        changes: 变更项列表
        test_requirements: 测试需求
        sandbox_level: 沙箱级别 (``worktree`` | ``tempdir`` | ``docker``)
        rollback_plan: 回滚计划描述
        created_at: 创建时间
    """

    task_id: str
    target_object_id: str
    target_platform: str
    target_path: str
    current_version: str
    requirement: str
    changes: list[ChangeItem] = field(default_factory=list)
    test_requirements: TestRequirements = field(default_factory=TestRequirements)
    sandbox_level: str = "tempdir"
    rollback_plan: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @staticmethod
    def generate_task_id() -> str:
        """生成唯一任务 ID."""
        return f"ila-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "target_object_id": self.target_object_id,
            "target_platform": self.target_platform,
            "target_path": self.target_path,
            "current_version": self.current_version,
            "requirement": self.requirement,
            "changes": [c.to_dict() for c in self.changes],
            "test_requirements": self.test_requirements.to_dict(),
            "sandbox_level": self.sandbox_level,
            "rollback_plan": self.rollback_plan,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        changes = [ChangeItem(**c) for c in data.get("changes", [])]
        test_req = TestRequirements(**data.get("test_requirements", {}))
        return cls(
            task_id=data["task_id"],
            target_object_id=data["target_object_id"],
            target_platform=data["target_platform"],
            target_path=data["target_path"],
            current_version=data.get("current_version", "unknown"),
            requirement=data["requirement"],
            changes=changes,
            test_requirements=test_req,
            sandbox_level=data.get("sandbox_level", "tempdir"),
            rollback_plan=data.get("rollback_plan", ""),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )
