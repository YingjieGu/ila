"""ILA CLI 入口 — 命令行接口."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import yaml

from ila.adapters.registry import AdapterRegistry
from ila.core.orchestrator import ILAOrchestrator


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """加载 ILA 配置."""
    if config_path is None:
        # 查找配置文件
        candidates = [
            os.path.expanduser("~/.ila/config.yaml"),
            os.path.join(os.path.dirname(__file__), "..", "..", "config", "ila_config.yaml"),
        ]
        for path in candidates:
            if os.path.exists(path):
                config_path = path
                break

    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def init_adapters(config: dict[str, Any]) -> None:
    """初始化平台适配器."""
    adapters_config = config.get("adapters", {})

    # Hermes
    hermes_cfg = adapters_config.get("hermes", {})
    if hermes_cfg.get("enabled", True):
        try:
            from ila.adapters.hermes_adapter import HermesAdapter
            adapter = HermesAdapter(
                hermes_home=hermes_cfg.get("hermes_home", "~/.hermes"),
                staging_profile=hermes_cfg.get("staging_profile", "ila-test"),
            )
            AdapterRegistry.register(adapter)
        except Exception as e:
            logging.warning("Hermes 适配器初始化失败: %s", e)

    # OpenClaw (v1.1)
    openclaw_cfg = adapters_config.get("openclaw", {})
    if openclaw_cfg.get("enabled", False):
        try:
            from ila.adapters.openclaw_adapter import OpenClawAdapter
            adapter = OpenClawAdapter(
                openclaw_home=openclaw_cfg.get("openclaw_home", "~/.openclaw"),
            )
            AdapterRegistry.register(adapter)
        except ImportError:
            logging.info("OpenClaw 适配器未安装 (v1.1)")

    # Custom (v2.0)
    custom_cfg = adapters_config.get("custom", {})
    if custom_cfg.get("enabled", False):
        logging.info("自定义适配器需手动注册")


def init_sandbox_manager(config: dict[str, Any]) -> Any:
    """初始化沙箱管理器."""
    from ila.sandbox.manager import SandboxManager
    sandbox_cfg = config.get("sandbox", {})
    return SandboxManager(
        workspace_root=sandbox_cfg.get("workspace_root", "/tmp"),
    )


def cmd_discover(args, config, orchestrator):
    """发现纳管对象."""
    if args.all:
        objects = orchestrator.discover()
    elif args.platform:
        objects = orchestrator.discover(platform=args.platform)
    else:
        objects = orchestrator.discover()

    if not objects:
        print("未发现任何纳管对象。")
        if args.platform:
            print(f"  平台: {args.platform}")
        print("  确保平台适配器已启用且平台已安装。")
        return

    print(f"\n发现 {len(objects)} 个纳管对象:\n")
    print(f"{'对象ID':<40} {'类型':<10} {'版本':<12} {'路径'}")
    print("-" * 100)
    for obj in objects:
        print(f"{obj['object_id']:<40} {obj['object_type']:<10} "
              f"{obj['current_version']:<12} {obj['path']}")


def cmd_run(args, config, orchestrator):
    """执行迭代闭环."""
    result = orchestrator.run_iteration(
        target_object_id=args.object_id,
        requirement=args.requirement,
        auto_approve=args.yes,
    )

    print(f"\n{'═' * 60}")
    print(f"  ILA 迭代结果: {result['status']}")
    print(f"{'═' * 60}")

    if result["status"] == "success":
        report = result.get("report", {})
        md = report.get("markdown", "")
        print(md)
        if "saved_reports" in result:
            print("\n报告已保存:")
            for fmt, path in result["saved_reports"].items():
                print(f"  {fmt}: {path}")
    elif result["status"] == "pending_approval":
        print(f"\n任务: {result['task_spec']['task_id']}")
        print(f"目标: {result['task_spec']['target_object_id']}")
        print(f"测试判定: {result['test_results']['verdict']}")
        print(f"\n测试通过，等待确认热切换。")
        print(f"使用以下命令完成热切换:")
        print(f"  ila swap {args.object_id} {result['sandbox_path']}")
    elif result["status"] == "test_failed":
        print(f"\n测试未通过: {result['results']['verdict']}")
        print(f"  通过: {result['results']['passed_cases']}/{result['results']['total_cases']}")
        print(f"  回归: {result['results']['regression_count']}")
    elif result["status"] == "develop_failed":
        print(f"\n开发失败: {result.get('reason', 'unknown')}")
    elif result["status"] == "rolled_back":
        print(f"\n热切换失败，已自动回滚")
        print(f"  原因: {result['swap_result'].get('reason', 'unknown')}")
    else:
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")


def cmd_swap(args, config, orchestrator):
    """仅执行热切换."""
    platform = args.object_id.split(":")[0]
    try:
        adapter = AdapterRegistry.get_adapter(platform)
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    obj = adapter.get_object(args.object_id)
    if not obj:
        print(f"错误: 对象不存在: {args.object_id}")
        sys.exit(1)

    from ila.core.switcher import Switcher
    switcher = Switcher(adapter)
    result = switcher.switch(obj, args.sandbox_path)

    print(f"\n热切换结果: {result['status']}")
    if result["status"] == "success":
        print(f"  快照: {result.get('snapshot', 'N/A')}")
    elif "reason" in result:
        print(f"  原因: {result['reason']}")


def cmd_rollback(args, config, orchestrator):
    """回滚."""
    result = orchestrator.rollback(args.object_id)
    print(f"\n回滚结果: {result['status']}")
    if "reason" in result:
        print(f"  原因: {result['reason']}")


def cmd_status(args, config, orchestrator):
    """查看状态."""
    stats = orchestrator.status()
    print(f"\nILA 状态:")
    print(f"  平台数:     {stats['platforms']}")
    print(f"  纳管对象:   {stats['objects']}")
    print(f"  版本记录:   {stats['total_versions']}")
    print(f"  上线版本:   {stats['live_versions']}")
    print(f"  测试用例:   {stats['test_cases']}")
    print(f"  自迭代版本: {stats['self_versions']}")

    platforms = AdapterRegistry.get_registered_platforms()
    if platforms:
        print(f"\n  已注册平台: {', '.join(platforms)}")


def cmd_test(args, config, orchestrator):
    """仅执行测试."""
    platform = args.object_id.split(":")[0]
    try:
        adapter = AdapterRegistry.get_adapter(platform)
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    obj = adapter.get_object(args.object_id)
    if not obj:
        print(f"错误: 对象不存在: {args.object_id}")
        sys.exit(1)

    from ila.core.tester import ABTester
    tester = ABTester(adapter)
    test_cases = tester.generate_default_test_cases(obj, {})
    result = tester.test(obj, args.sandbox_path, test_cases)

    print(f"\n测试结果: {result.verdict}")
    print(f"  通过: {result.passed_cases}/{result.total_cases}")
    print(f"  回归: {result.regression_count}")
    print(f"\n{result.summary}")


def main():
    """CLI 主入口."""
    parser = argparse.ArgumentParser(
        prog="ila",
        description="ILA: Iteration Loop Agent — 平台无关的敏捷迭代闭环智能体",
    )
    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # discover
    p_discover = subparsers.add_parser("discover", help="发现纳管对象")
    p_discover.add_argument("--platform", "-p", help="指定平台")
    p_discover.add_argument("--all", "-a", action="store_true", help="跨平台发现")

    # run
    p_run = subparsers.add_parser("run", help="执行迭代闭环")
    p_run.add_argument("object_id", help="目标对象 ID (e.g. hermes:skill:my-skill)")
    p_run.add_argument("requirement", help="需求描述")
    p_run.add_argument("--yes", "-y", action="store_true", help="自动批准热切换")

    # test
    p_test = subparsers.add_parser("test", help="仅执行 A/B 测试")
    p_test.add_argument("object_id", help="目标对象 ID")
    p_test.add_argument("sandbox_path", help="沙箱路径")

    # swap
    p_swap = subparsers.add_parser("swap", help="仅执行热切换")
    p_swap.add_argument("object_id", help="目标对象 ID")
    p_swap.add_argument("sandbox_path", help="沙箱路径")

    # rollback
    p_rollback = subparsers.add_parser("rollback", help="回滚到上一版本")
    p_rollback.add_argument("object_id", help="目标对象 ID")

    # status
    subparsers.add_parser("status", help="查看 ILA 状态")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # 设置日志
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # 加载配置
    config = load_config(args.config)

    # 初始化
    init_adapters(config)
    sandbox_manager = init_sandbox_manager(config)
    orchestrator = ILAOrchestrator(config, sandbox_manager=sandbox_manager)

    # 分发命令
    commands = {
        "discover": cmd_discover,
        "run": cmd_run,
        "test": cmd_test,
        "swap": cmd_swap,
        "rollback": cmd_rollback,
        "status": cmd_status,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args, config, orchestrator)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
