"""闭环编排器 — ILA 核心引擎，串联六阶段迭代闭环."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

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
        self._pending_approval: dict | None = None  # pending_approval 状态缓存

    def run_iteration(self, target_object_id: str, requirement: str,
                      auto_approve: bool | None = None,
                      test_cases: list[dict] | None = None,
                      progress_callback: Callable[[str, str, str | None], None] | None = None
                      ) -> dict[str, Any]:
        """执行完整的迭代闭环.

        Args:
            target_object_id: 目标对象 ID (e.g. ``hermes:skill:my-skill``)
            requirement: 自然语言需求描述
            auto_approve: 是否自动批准热切换 (覆盖配置)
            test_cases: 自定义测试用例 (可选)
            progress_callback: 进度回调 ``callback(phase, status, detail)``
                phase: analyze|develop|test|verify|switch
                status: running|done|failed|skipped
                detail: 可选的描述文本

        Returns:
            完整结果字典:
            - ``{"status": "success", "report": {...}}``
            - ``{"status": "test_failed", "results": {...}}``
            - ``{"status": "develop_failed", "reason": "..."}``
            - ``{"status": "rolled_back", ...}``
            - ``{"status": "cancelled_by_user"}``
            - ``{"status": "error", "reason": "..."}``
        """
        def _notify(phase: str, status: str, detail: str | None = None,
                    context: dict | None = None):
            if progress_callback:
                try:
                    progress_callback(phase, status, detail, context)
                except Exception:
                    pass

        # 解析平台
        platform = target_object_id.split(":")[0]
        try:
            adapter = AdapterRegistry.get_adapter(platform)
        except ValueError as e:
            _notify("analyze", "failed", str(e))
            return {"status": "error", "reason": str(e)}

        # 获取目标对象
        obj = adapter.get_object(target_object_id)
        if not obj:
            _notify("analyze", "failed", f"对象不存在: {target_object_id}")
            return {"status": "error", "reason": f"对象不存在: {target_object_id}"}

        # 注册对象到版本注册表
        self.registry.register_object(obj)

        logger.info("═══ ILA 迭代闭环开始 ═══")
        logger.info("目标: %s", target_object_id)
        logger.info("需求: %s", requirement)

        # ===== Phase 1: 需求分析 =====
        logger.info("━━━ Phase 1: 需求分析 ━━━")
        _notify("analyze", "running", "正在解析需求并生成任务规格书...")
        analyzer = Analyzer(adapter)
        task_spec = analyzer.analyze(obj, requirement)

        # 创建版本记录 (使用语义版本号)
        next_version = self.registry.get_next_version(obj.object_id)
        version_id = self.registry.create_version(
            obj.object_id, next_version, task_spec=task_spec.to_dict()
        )
        task_spec.task_id = task_spec.task_id  # 保持 task_id
        logger.info("任务规格书: %s", task_spec.task_id)
        _notify("analyze", "done", f"任务规格书: {task_spec.task_id}")

        # ===== Phase 2: 沙箱开发 =====
        logger.info("━━━ Phase 2: 沙箱开发 ━━━")
        _notify("develop", "running", "创建隔离沙箱，调用 Codex CLI 开发...")
        if not self.sandbox_manager:
            _notify("develop", "failed", "沙箱管理器未初始化")
            return {"status": "error", "reason": "沙箱管理器未初始化"}

        dev_config = self.config.get("sandbox", {})
        developer = Developer(adapter, self.sandbox_manager, dev_config)
        dev_result = developer.develop(obj, task_spec)

        if dev_result["status"] != "success":
            self.registry.update_version_status(version_id, "failed")
            _notify("develop", "failed", dev_result.get("reason", "开发失败"))
            return {
                "status": "develop_failed",
                "reason": dev_result.get("reason", "开发失败"),
                "task_spec": task_spec.to_dict(),
            }

        sandbox_path = dev_result["sandbox_path"]
        logger.info("沙箱开发完成: %s", sandbox_path)
        _notify("develop", "done", f"沙箱: {sandbox_path}",
                {"sandbox_path": sandbox_path})

        # ===== Phase 3: A/B 对比测试 =====
        logger.info("━━━ Phase 3: A/B 对比测试 ━━━")
        _notify("test", "running", "新旧版本对比测试中...")
        tester = ABTester(
            adapter,
            timeout=self.config.get("test", {}).get("default_timeout", 60),
            performance_threshold=self.config.get("test", {}).get("performance_threshold", 1.2),
        )

        if not test_cases:
            test_cases = tester.generate_default_test_cases(
                obj, task_spec.test_requirements.to_dict()
            )

        # 提取原始服务目录地址
        source_dir = getattr(obj, 'path', '') or getattr(obj, 'source_path', '')
        if not source_dir:
            source_dir = getattr(adapter, 'src_dir', '') or getattr(adapter, 'project_root', '')

        test_result = tester.test(obj, sandbox_path, test_cases)
        test_result.task_id = task_spec.task_id

        self.registry.update_version_status(
            version_id, "testing",
            test_results=test_result.to_dict(),
        )

        logger.info("测试判定: %s", test_result.verdict)
        _notify("test", "done" if test_result.verdict in ("pass", "degraded") else "failed",
                f"判定: {test_result.verdict} ({test_result.passed_cases}/{test_result.total_cases})")

        if test_result.verdict not in ("pass", "degraded"):
            return {
                "status": "test_failed",
                "results": test_result.to_dict(),
                "task_spec": task_spec.to_dict(),
                "sandbox_path": sandbox_path,
                "source_dir": source_dir,
            }

        # ===== Phase 4: 部署验证 =====
        logger.info("━━━ Phase 4: 部署验证 ━━━")
        _notify("verify", "running", "兼容性检查中...")
        deploy_result = self._verify_deployment(adapter, obj, sandbox_path)
        if not deploy_result["passed"]:
            _notify("verify", "failed", str(deploy_result.get("issues", [])))
            return {
                "status": "verification_failed",
                "result": deploy_result,
                "task_spec": task_spec.to_dict(),
                "sandbox_path": sandbox_path,
                "source_dir": source_dir,
                "original_url": "http://localhost:9527",
            }
        _notify("verify", "done", f"兼容性检查通过, 验证地址: {deploy_result.get('staging_url', '')}",
                {"source_dir": source_dir, "sandbox_path": sandbox_path,
                 "staging_url": deploy_result.get("staging_url", ""),
                 "original_url": "http://localhost:9527"})

        # ===== Phase 5: 热切换上线 =====
        logger.info("━━━ Phase 5: 热切换上线 ━━━")
        should_approve = auto_approve if auto_approve is not None else self.auto_approve
        if not should_approve:
            staging_url = deploy_result.get("staging_url", "http://localhost:9528")
            logger.info("需要用户确认热切换 (配置 auto_approve=false)")
            prompt_msg = f"🔗 请在浏览器打开 {staging_url} 验证新版本，然后在面板点击「批准」或「跳过」"
            _notify("switch", "skipped", prompt_msg)
            # 缓存 pending 状态供 approve_switch 使用
            self._pending_approval = {
                "target_object_id": target_object_id,
                "requirement": requirement,
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
                "deploy_result": deploy_result,
                "sandbox_path": sandbox_path,
                "version_id": version_id,
                "obj": obj,
                "adapter": adapter,
            }
            return {
                "status": "pending_approval",
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
                "deploy_result": deploy_result,
                "sandbox_path": sandbox_path,
                "version_id": version_id,
                "source_dir": source_dir,
                "original_url": "http://localhost:9527",
                "staging_url": deploy_result.get("staging_url", ""),
            }

        _notify("switch", "running", "正式上线: 关闭旧版, 新版迁移到 9527...")
        staging_id = deploy_result.get("staging_id")
        promote_fn = getattr(adapter, 'promote_staging', None)
        # 如果 deploy_result 缺少 staging_id，尝试从 staging 目录查找
        if promote_fn and not staging_id:
            staging_dir = os.path.expanduser("~/.ila/staging")
            if os.path.isdir(staging_dir):
                staging_files = sorted(
                    [f for f in os.listdir(staging_dir) if f.endswith(".json")],
                    reverse=True
                )
                if staging_files:
                    staging_id = staging_files[0].rsplit(".", 1)[0]
                    logger.info("从 staging 目录恢复 staging_id: %s", staging_id)
        if promote_fn and staging_id:
            swap_result = promote_fn(staging_id)
        else:
            switcher = Switcher(adapter)
            swap_result = switcher.switch(obj, sandbox_path)

        swap_status = swap_result.get("status", "error")

        # ── Launcher 委托模式 (v2): "promoting" / "sent" 表示已委托 Launcher ──
        if swap_status in ("promoting", "sent", "dispatched"):
            logger.info("已委托 Launcher 执行升级")
            self.registry.update_version_status(
                version_id, "live",
                deploy_verification=deploy_result,
            )
            _notify("switch", "done", "已委托 Launcher 执行升级")

            # 生成报告
            logger.info("━━━ 生成报告 ━━━")
            report = self.reporter.generate(
                obj, task_spec, test_result.to_dict(), deploy_result, swap_result
            )
            report_dir = os.path.expanduser(
                self.config.get("report", {}).get("output_dir", "~/.ila/reports")
            )
            saved = self.reporter.save_report(report, report_dir, task_spec.task_id)

            logger.info("═══ ILA 迭代闭环完成 (委托 Launcher 热切换) ═══")
            return {
                "status": "success",
                "message": "新版本正在由 Launcher 部署上线",
                "swap_result": swap_result,
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
                "report": report,
                "saved_reports": saved,
                "source_dir": source_dir,
                "sandbox_path": sandbox_path,
                "publish_info": {
                    "platform": obj.platform,
                    "object_type": obj.object_type,
                    "object_id": obj.object_id,
                    "path": obj.path,
                    "note": f"已发布为 {obj.platform} 平台的 {obj.object_type} 能力 ({obj.object_id}), 位置: {obj.path}",
                },
            }

        self.registry.update_version_status(
            version_id,
            "live" if swap_status == "success" else "rolled_back",
            deploy_verification=deploy_result,
            rollback_snapshot=swap_result.get("snapshot"),
        )

        if swap_status != "success":
            # Phase 6 已在适配器/Switcher 中自动处理
            logger.warning("热切换失败，已回滚")
            _notify("switch", "failed", f"热切换失败，已回滚: {swap_result.get('reason', '')}")
            return {
                "status": "rolled_back",
                "swap_result": swap_result,
                "task_spec": task_spec.to_dict(),
                "test_results": test_result.to_dict(),
                "source_dir": source_dir,
                "sandbox_path": sandbox_path,
            }

        _notify("switch", "done", "热切换成功，新版本已上线")

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
            "source_dir": source_dir,
            "sandbox_path": sandbox_path,
            "original_url": "http://localhost:9527",
        }

    def approve_switch(self, target_object_id: str,
                       progress_callback: Callable[[str, str, str | None], None] | None = None
                       ) -> dict[str, Any]:
        """批准热切换，继续执行上线和报告生成.

        Args:
            target_object_id: 目标对象 ID
            progress_callback: 进度回调

        Returns:
            与 run_iteration 相同的格式
        """
        def _notify(phase: str, status: str, detail: str | None = None,
                    context: dict | None = None):
            if progress_callback:
                try:
                    progress_callback(phase, status, detail, context)
                except Exception:
                    pass

        pending = self._pending_approval
        if not pending:
            return {"status": "error", "reason": "没有待批准的热切换"}

        obj = pending["obj"]
        adapter = pending["adapter"]
        sandbox_path = pending["sandbox_path"]
        version_id = pending["version_id"]
        task_spec_dict = pending["task_spec"]
        test_results_dict = pending["test_results"]
        deploy_result = pending["deploy_result"]

        # 执行热切换 (正式上线: 停旧版, 新版迁移到 9527)
        _notify("switch", "running", "正式上线: 关闭旧版, 新版迁移到 9527...")
        staging_id = deploy_result.get("staging_id")
        promote_fn = getattr(adapter, 'promote_staging', None)
        # 如果 deploy_result 缺少 staging_id，尝试从 staging 目录查找
        if promote_fn and not staging_id:
            staging_dir = os.path.expanduser("~/.ila/staging")
            if os.path.isdir(staging_dir):
                staging_files = sorted(
                    [f for f in os.listdir(staging_dir) if f.endswith(".json")],
                    reverse=True
                )
                if staging_files:
                    staging_id = staging_files[0].rsplit(".", 1)[0]
                    logger.info("从 staging 目录恢复 staging_id: %s", staging_id)
        if promote_fn and staging_id:
            swap_result = promote_fn(staging_id)
        else:
            switcher = Switcher(adapter)
            swap_result = switcher.switch(obj, sandbox_path)

        swap_status = swap_result.get("status", "error")

        # ── Launcher 委托模式 (v2): "promoting" / "sent" 表示已委托 Launcher ──
        if swap_status in ("promoting", "sent", "dispatched"):
            # Launcher 已接收命令，将在后台执行实际重启
            command_id = swap_result.get("command_id", "unknown")
            logger.info("已委托 Launcher 执行升级: command_id=%s", command_id)

            self.registry.update_version_status(
                version_id, "live",
                deploy_verification=deploy_result,
            )

            _notify("switch", "done",
                    f"已委托 Launcher 执行升级 (command_id={command_id})")

            self._pending_approval = None
            logger.info("═══ ILA 迭代闭环完成 (委托 Launcher 热切换) ═══")
            return {
                "status": "success",
                "swap_result": swap_result,
                "task_spec": task_spec_dict,
                "test_results": test_results_dict,
                "message": "新版本正在由 Launcher 部署上线",
            }

        # ── 传统模式: 错误处理 ──
        if swap_status != "success":
            logger.warning("热切换失败，已回滚")
            self.registry.update_version_status(
                version_id, "rolled_back",
                deploy_verification=deploy_result,
                rollback_snapshot=swap_result.get("snapshot"),
            )
            _notify("switch", "failed",
                    f"热切换失败，已回滚: {swap_result.get('reason', '')}")
            self._pending_approval = None
            return {
                "status": "rolled_back",
                "swap_result": swap_result,
                "task_spec": task_spec_dict,
                "test_results": test_results_dict,
            }

        # ── 传统模式: 直接成功 ──
        self.registry.update_version_status(
            version_id, "live",
            deploy_verification=deploy_result,
            rollback_snapshot=swap_result.get("snapshot"),
        )

        _notify("switch", "done", "热切换成功，新版本已上线")

        # 重建 TaskSpec 用于报告
        from ila.models.task_spec import TaskSpec
        task_spec = TaskSpec.from_dict(task_spec_dict)

        # 生成报告 (test_results 直接传 dict)
        logger.info("━━━ 生成报告 ━━━")
        report = self.reporter.generate(
            obj, task_spec, test_results_dict, deploy_result, swap_result
        )
        report_dir = os.path.expanduser(
            self.config.get("report", {}).get("output_dir", "~/.ila/reports")
        )
        saved = self.reporter.save_report(report, report_dir, task_spec.task_id)

        self._pending_approval = None
        logger.info("═══ ILA 迭代闭环完成 (批准热切换) ═══")
        return {
            "status": "success",
            "report": report,
            "saved_reports": saved,
            "task_spec": task_spec_dict,
            "test_results": test_results_dict,
            "swap_result": swap_result,
        }

    def skip_switch(self) -> dict[str, Any]:
        """跳过热切换，清理 pending 状态.

        Returns:
            {"status": "skipped", "reason": "用户跳过热切换"}
        """
        if self._pending_approval:
            self._pending_approval = None
        return {"status": "skipped", "reason": "用户跳过热切换"}

    def _verify_deployment(self, adapter: PlatformAdapter,
                           obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """Phase 4: 部署验证 + 部署到 staging 环境.

        流程:
          1. 兼容性检查
          2. 部署到 staging (启动新进程在 9528)
          3. 打开浏览器供用户验证 (AB 模式)
        """
        result = {}
        try:
            compat = adapter.validate_compatibility(obj, sandbox_path)
            result = {
                "passed": compat.get("compatible", True),
                "compatibility": compat,
                "issues": compat.get("issues", []),
                "warnings": compat.get("warnings", []),
            }
        except Exception as e:
            return {"passed": False, "reason": f"验证异常: {e}"}

        if not result["passed"]:
            return result

        # 尝试部署到 staging (验证新版本是否能正常启动)
        try:
            deploy_result = adapter.deploy_to_staging(obj, sandbox_path)
            if isinstance(deploy_result, dict):
                result["staging_id"] = deploy_result.get("staging_id", "")
                result["staging_url"] = deploy_result.get("staging_url", "")
            else:
                result["staging_id"] = deploy_result
            result["staging_deployed"] = True

            # 若 adapter 未提供 staging_url，使用默认端口
            if not result.get("staging_url"):
                staging_port = getattr(adapter, 'staging_port', 9528)
                result["staging_url"] = f"http://localhost:{staging_port}"

            logger.info("新版已部署到 staging: %s", result.get("staging_url"))
            logger.info("请在浏览器中打开 %s 验证新版本，然后返回 ILA Dashboard 批准或跳过热切换",
                        result.get("staging_url"))

        except Exception as e:
            logger.warning("部署到 staging 失败 (不影响验证结果): %s", e)
            result["staging_deploy_error"] = str(e)
            result["warnings"] = list(result.get("warnings", [])) + [f"部署到 staging 失败: {e}"]

        return result

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
