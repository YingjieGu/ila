"""闭环编排器 — ILA 核心引擎，串联六阶段迭代闭环."""

from __future__ import annotations

import logging
import os
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.adapters.registry import AdapterRegistry
from ila.core.analyzer import Analyzer
from ila.core.developer import Developer
from ila.core.registry import VersionRegistry
from ila.core.reporter import Reporter
from ila.core.switcher import Roller, Switcher
from ila.core.tester import ABTester
from ila.models.managed_object import ManagedObject
from ila.models.task_spec import TaskSpec

logger = logging.getLogger(__name__)


class ILAOrchestrator:
    """ILA 闭环编排器 — 核心引擎.

    串联六阶段: 需求分析 → 沙箱开发 → A/B对比测试 → 部署验证 → 热切换上线 → 回滚兜底
    平台无关，通过 PlatformAdapter 操作具体平台。
    """

    def __init__(self, config: dict, sandbox_manager: Any | None = None):
        """初始化编排器.

        Args:
            config: ILA 配置字典
            sandbox_manager: 沙箱管理器实例 (可选，延迟初始化)
        """
        self.config = config
        self.ila_home = os.path.expanduser(config.get("ila", {}).get("home", "~/.ila"))
        self.registry = VersionRegistry(ila_home=self.ila_home)
        self.sandbox_manager = sandbox_manager
        self.auto_approve = config.get("ila", {}).get("auto_approve", False)
        self.reporter = Reporter()

    def run_iteration(self, target_object_id: str, requirement: str,
                      auto_approve: bool | None = None,
                      test_cases: list[dict] | None = None) -> dict[str, Any]:
        """执行完整的迭代闭环.

        Args:
            target_object_id: 目标对象 ID (e.g. ``hermes:skill:my-skill``)
            requirement: 自然语言需求描述
            auto_approve: 是否自动批准热切换 (覆盖配置)
            test_cases: 自定义测试用例 (可选)

        Returns:
            完整结果字典:
            - ``{"status": "success", "report": {...}}``
            - ``{"status": "test_failed", "results": {...}}``
            - ``{"status": "develop_failed", "reason": "..."}``
            - ``{"status": "rolled_back", ...}``
            - ``{"status": "cancelled_by_user"}``
            - ``{"status": "error", "reason": "..."}``
        """
        # 解析平台
        platform = target_object_id.split(":")[0]
        try:
            adapter = AdapterRegistry.get_adapter(platform)
        except ValueError as e:
            return {"status": "error", "reason": str(e)}

        # 获取目标对象
        obj = adapter.get_object(target_object_id)
        if not obj:
            return {"status": "error", "reason": f"对象不存在: {target_object_id}"}

        # 注册对象到版本注册表
        self.registry.register_object(obj)

        logger.info("═══ ILA 迭代闭环开始 ═══")
        logger.info("目标: %s", target_object_id)
        logger.info("需求: %s", requirement)

        # ===== Phase 1: 需求分析 =====
        logger.info("━━━ Phase 1: 需求分析 ━━━")
        analyzer = Analyzer(adapter)
        task_spec = analyzer.analyze(obj, requirement)

        # 创建版本记录
        version_id = self.registry.create_version(
            obj.object_id, "pending", task_spec=task_spec.to_dict()
        )
        task_spec.task_id = task_spec.task_id  # 保持 task_id
        logger.info("任务规格书: %s", task_spec.task_id)

        # ===== Phase 2: 沙箱开发 =====
        logger.info("━━━ Phase 2: 沙箱开发 ━━━")
        if not self.sandbox_manager:
            return {"status": "error", "reason": "沙箱管理器未初始化"}

        dev_config = self.config.get("sandbox", {})
        developer = Developer(adapter, self.sandbox_manager, dev_config)
        dev_result = developer.develop(obj, task_spec)

        if dev_result["status"] != "success":
            self.registry.update_version_status(version_id, "failed")
            return {
                "status": "develop_failed",
                "reason": dev_result.get("reason", "开发失败"),
                "task_spec": task_spec.to_dict(),
            }

        sandbox_path = dev_result["sandbox_path"]
        logger.info("沙箱开发完成: %s", sandbox_path)

        # ===== Phase 3: A/B 对比测试 =====
        logger.info("━━━ Phase 3: A/B 对比测试 ━━━")
        tester = ABTester(
            adapter,
            timeout=self.config.get("test", {}).get("default_timeout", 60),
            performance_threshold=self.config.get("test", {}).get("performance_threshold", 1.2),
        )

        if not test_cases:
            test_cases = tester.generate_default_test_cases(
                obj, task_spec.test_requirements.to_dict()
            )

        test_result = tester.test(obj, sandbox_path, test_cases)
        test_result.task_id = task_spec.task_id

        self.registry.update_version_status(
            version_id, "testing",
            test_results=test_result.to_dict(),
        )

        logger.info("测试判定: %s", test_result.verdict)

        if test_result.verdict not in ("pass", "degraded"):
            return {
                "status": "test_failed",
                "results": test_result.to_dict(),
                "task_spec": task_spec.to_dict(),
                "sandbox_path": sandbox_path,
            }

        # ===== Phase 4: 部署验证 =====
        logger.info("━━━ Phase 4: 部署验证 ━━━")
        deploy_result = self._verify_deployment(adapter, obj, sandbox_path)
        if not deploy_result["passed"]:
            return {
                "status": "verification_failed",
                "result": deploy_result,
                "task_spec": task_spec.to_dict(),
                "sandbox_path": sandbox_path,
            }

        # ===== Phase 5: 热切换上线 =====
        logger.info("━━━ Phase 5: 热切换上线 ━━━")
        should_approve = auto_approve if auto_approve is not None else self.auto_approve
        if not should_approve:
            logger.info("需要用户确认热切换 (配置 auto_approve=false)")
            # 在非交互模式下，我们暂停等待用户确认
            # 这里返回待确认状态
            return {
                "status": "pending_approval",
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
                "deploy_result": deploy_result,
                "sandbox_path": sandbox_path,
                "version_id": version_id,
            }

        switcher = Switcher(adapter)
        swap_result = switcher.switch(obj, sandbox_path)

        self.registry.update_version_status(
            version_id,
            "live" if swap_result["status"] == "success" else "rolled_back",
            deploy_verification=deploy_result,
            rollback_snapshot=swap_result.get("snapshot"),
        )

        if swap_result["status"] != "success":
            # Phase 6 已在适配器/Switcher 中自动处理
            logger.warning("热切换失败，已回滚")
            return {
                "status": "rolled_back",
                "swap_result": swap_result,
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
            }

        # ===== 生成报告 =====
        logger.info("━━━ 生成报告 ━━━")
        report = self.reporter.generate(
            obj, task_spec, test_result.to_dict(), deploy_result, swap_result
        )

        # 保存报告
        report_dir = os.path.expanduser(
            self.config.get("report", {}).get("output_dir", "~/.ila/reports")
        )
        saved = self.reporter.save_report(report, report_dir, task_spec.task_id)

        logger.info("═══ ILA 迭代闭环完成 ═══")
        return {
            "status": "success",
            "report": report,
            "saved_reports": saved,
            "task_spec": task_spec.to_dict(),
            "test_results": test_result.to_dict(),
            "swap_result": swap_result,
        }

    def _verify_deployment(self, adapter: PlatformAdapter,
                           obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """Phase 4: 部署验证."""
        try:
            compat = adapter.validate_compatibility(obj, sandbox_path)
            return {
                "passed": compat.get("compatible", True),
                "compatibility": compat,
                "issues": compat.get("issues", []),
                "warnings": compat.get("warnings", []),
            }
        except Exception as e:
            return {"passed": False, "reason": f"验证异常: {e}"}

    def rollback(self, target_object_id: str) -> dict[str, Any]:
        """手动回滚到上一版本.

        Args:
            target_object_id: 目标对象 ID

        Returns:
            回滚结果
        """
        platform = target_object_id.split(":")[0]
        try:
            adapter = AdapterRegistry.get_adapter(platform)
        except ValueError as e:
            return {"status": "error", "reason": str(e)}

        obj = adapter.get_object(target_object_id)
        if not obj:
            return {"status": "error", "reason": f"对象不存在: {target_object_id}"}

        # 从版本注册表获取最新快照
        versions = self.registry.get_versions_by_object(target_object_id)
        snapshot_path = None
        for v in versions:
            if v.get("rollback_snapshot"):
                snapshot_path = v["rollback_snapshot"]
                break

        if not snapshot_path:
            return {"status": "error", "reason": "没有可用的回滚快照"}

        roller = Roller(adapter)
        result = roller.rollback(obj, snapshot_path)

        if result["status"] == "success":
            self.registry.update_object_version(target_object_id, "rolled_back")

        return result

    def discover(self, platform: str | None = None) -> list[dict[str, Any]]:
        """发现纳管对象."""
        if platform:
            try:
                adapter = AdapterRegistry.get_adapter(platform)
                objects = adapter.discover_objects()
            except ValueError:
                return []
        else:
            objects = AdapterRegistry.discover_all_objects()

        # 注册到版本注册表
        for obj in objects:
            self.registry.register_object(obj)

        return [obj.to_dict() for obj in objects]

    def status(self) -> dict[str, Any]:
        """获取 ILA 状态."""
        return self.registry.get_stats()
