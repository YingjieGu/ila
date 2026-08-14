"""ILA CLI 入口 — 命令行接口."""

# SKILL.md: 技能配置文件格式，定义技能元数据与行为规范
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


def _ensure_utf8_stdio() -> None:
    """Windows GBK 控制台下强制 UTF-8 输出，避免中文 print 抛 UnicodeEncodeError."""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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
        with open(config_path, encoding="utf-8") as f:
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

    # WorkBuddy (v1.1)
    workbuddy_cfg = adapters_config.get("workbuddy", {})
    if workbuddy_cfg.get("enabled", False):
        try:
            from ila.adapters.workbuddy_adapter import WorkBuddyAdapter
            adapter = WorkBuddyAdapter(
                workbuddy_home=workbuddy_cfg.get("workbuddy_home", "~/.workbuddy"),
            )
            AdapterRegistry.register(adapter)
        except ImportError:
            logging.info("WorkBuddy 适配器未安装 (v1.1)")

    # ILA 自升级适配器 (v2.0)
    ila_cfg = adapters_config.get("ila", {})
    if ila_cfg.get("enabled", True):
        try:
            from ila.adapters.ila_self_adapter import IlaSelfAdapter
            adapter = IlaSelfAdapter(
                # project_root 未配置时自动检测 (基于 __file__), 兼容任意安装位置
                project_root=ila_cfg.get("project_root"),
                dashboard_port=ila_cfg.get("dashboard_port", 9527),
                staging_port=ila_cfg.get("staging_port", 9528),
            )
            AdapterRegistry.register(adapter)
        except Exception as e:
            logging.warning("ILA 自升级适配器初始化失败: %s", e)

    # Custom (v2.0)
    custom_cfg = adapters_config.get("custom", {})
    if custom_cfg.get("enabled", False):
        logging.info("自定义适配器需手动注册")


def init_sandbox_manager(config: dict[str, Any]) -> Any:
    """初始化沙箱管理器."""
    from ila.sandbox.manager import SandboxManager
    sandbox_cfg = config.get("sandbox", {})
    return SandboxManager(
        workspace_root=sandbox_cfg.get("workspace_root"),
    )


def cmd_discover(args, config, orchestrator):
    """发现纳管对象."""
    if args.all:
        objects = orchestrator.discover()
    elif args.platform:
        objects = orchestrator.discover(platform=args.platform)
    else:
        objects = orchestrator.discover()

    # 用注册表中最新迭代版本号同步各对象的 current_version
    try:
        from ila.core.registry import VersionRegistry
        _reg = VersionRegistry()
        for obj in objects:
            latest = _reg.get_latest_version(obj["object_id"])
            if latest and latest.get("version"):
                obj["current_version"] = latest["version"]
    except Exception:
        pass

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

    # 显示迭代上下文信息（目录和URL）
    source_dir = result.get("source_dir", "")
    sandbox_path = result.get("sandbox_path", "")
    staging_url = result.get("staging_url", "")
    original_url = result.get("original_url", "")
    if any([source_dir, sandbox_path, staging_url, original_url]):
        print(f"\n  📋 迭代上下文:")
        if source_dir:
            print(f"     原服务地址目录: {source_dir}")
        if sandbox_path:
            print(f"     沙箱目录地址:   {sandbox_path}")
        if original_url:
            print(f"     原服务URL:      {original_url}")
        if staging_url:
            print(f"     新服务URL:      {staging_url}")

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


def cmd_version(args, config, orchestrator):
    """版本历史操作 (按版本状态映射可用操作)."""
    from ila.core.registry import VersionRegistry
    ila_home = os.path.expanduser(config.get("ila", {}).get("home", "~/.ila"))
    registry = VersionRegistry(ila_home=ila_home)

    from ila import (
        VERSION_OPERATIONS,
        get_operation_label,
        get_operation_target_status,
        get_version_operations,
        is_valid_version_operation,
    )

    if getattr(args, "operations", False):
        print("\n版本状态可用操作:")
        for status, ops in VERSION_OPERATIONS.items():
            print(f"  [{status}]")
            for op in ops:
                print(f"    {op:14s} {get_operation_label(op)} -> {get_operation_target_status(op)}")
        return

    # D1: ila version --list — 列出所有版本记录
    if getattr(args, "list_versions", False) or (args.version_id == "list"):
        versions = registry.list_versions()
        if not versions:
            print("暂无版本记录")
            return
        print(f"\n共 {len(versions)} 条版本记录:\n")
        print(f"{'ID':<6} {'对象':<32} {'版本':<10} {'状态':<12} {'时间'}")
        print("-" * 80)
        for v in versions:
            ts = ""
            import datetime
            if v.get("created_at"):
                try:
                    ts = datetime.datetime.fromtimestamp(float(v["created_at"])).strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    ts = str(v.get("created_at"))[:16]
            print(f"{str(v.get('id','')):<6} {str(v.get('object_id','')):<32} "
                  f"{str(v.get('version','')):<10} {str(v.get('status','')):<12} {ts}")
        return

    if not args.version_id:
        print("错误: 需要指定版本 ID，或使用 --operations 查看可用操作，或 --list 查看版本列表")
        sys.exit(1)

    if not args.operate:
        print("错误: 需要使用 --operate 指定操作，或使用 --operations 查看可用操作")
        sys.exit(1)

    operation = args.operate
    if not is_valid_version_operation(operation):
        print(f"错误: 不支持的操作 '{operation}'")
        sys.exit(1)

    version = registry.get_version(args.version_id)
    if not version:
        print(f"错误: 版本不存在: {args.version_id}")
        sys.exit(1)

    status = version.get("status", "")
    if operation not in get_version_operations(status):
        print(f"错误: 版本状态 '{status}' 不支持操作 '{operation}'")
        allowed = get_version_operations(status)
        print(f"  允许的操作: {', '.join(allowed) if allowed else '无'}")
        sys.exit(1)

    object_id = version.get("object_id", "")

    if operation == "rollback":
        result = orchestrator.rollback(object_id)
        print(f"\n回滚结果: {result['status']}")
        if "reason" in result:
            print(f"  原因: {result['reason']}")
        return

    registry.update_version_status(
        args.version_id, get_operation_target_status(operation)
    )
    print(f"\n操作完成: {get_operation_label(operation)}")
    print(f"  版本 ID:   {args.version_id}")
    print(f"  目标状态:  {get_operation_target_status(operation)}")


def cmd_status(args, config, orchestrator):
    """查看状态."""
    stats = orchestrator.status()
    print(f"\nILA 状态:")
    print(f"  平台数:     {stats['platforms']}")
    print(f"  纳管对象:   {stats['objects']}")
    print(f"  版本记录:   {stats['total_versions']}")
    print(f"  上线版本:   {stats['live_versions']}")
    print(f"  测试用例:   {stats['test_cases']}")

    platforms = AdapterRegistry.get_registered_platforms()
    if platforms:
        print(f"\n  已注册平台: {', '.join(platforms)}")

    # D4: --verbose 展示各对象当前版本/状态
    if getattr(args, "verbose", False):
        from ila.core.registry import VersionRegistry
        ila_home = os.path.expanduser(config.get("ila", {}).get("home", "~/.ila"))
        registry = VersionRegistry(ila_home=ila_home)
        objects = orchestrator.discover()
        if objects:
            print(f"\n  对象明细 ({len(objects)} 个):")
            print(f"  {'对象ID':<42} {'版本':<12} {'状态'}")
            print("  " + "-" * 70)
            for obj in objects:
                oid = obj.get("object_id", "?")
                latest = registry.get_latest_version(oid)
                version = (latest or {}).get("version", obj.get("current_version", "?"))
                status = (latest or {}).get("status", "-")
                print(f"  {oid:<42} {str(version):<12} {status}")



def cmd_report(args, config, orchestrator):
    """查看迭代报告.

    - 无参数: 展示最近一次版本迭代报告
    - --version-list: 列出所有版本迭代报告
    - --version-id <id>: 查看指定版本迭代报告详情
    - --format json|markdown|text: 输出格式
    """
    from ila import get_version_report, list_version_reports

    if getattr(args, "version_report", False):
        _cmd_version_report(fmt=args.format)
        return

    if getattr(args, "version_id", None):
        _cmd_version_report_detail(args.version_id, fmt=args.format)
        return

    # D2: 无参数 → 展示最近一次迭代报告
    reports = list_version_reports()
    if not reports:
        print("暂无版本迭代报告。")
        print("提示: 使用 `ila report --version-list` 查看列表")
        return
    latest_task_id = reports[0].get("task_id")
    if latest_task_id:
        _cmd_version_report_detail(latest_task_id, fmt=args.format)
    else:
        _cmd_version_report(fmt=args.format)


def _cmd_version_report(fmt: str = "text") -> None:
    """显示版本迭代报告列表（含 ILA 自身及其他纳管对象）."""
    from ila import list_version_reports
    reports = list_version_reports()

    if not reports:
        print("暂无版本迭代报告。")
        return

    if fmt == "json":
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
        return

    # text format
    print(f"\n版本迭代报告 ({len(reports)} 条):\n")
    for r in reports:
        status_icon = "✅" if r.get("verdict") == "pass" else "❌"
        old_v = r.get("old_version", "?")
        new_v = r.get("new_version", "?")
        created = r.get("created_at", "?")
        task_id = r.get("task_id", "?")
        obj_name = r.get("object", {}).get("name", r.get("object", {}).get("object_id", "?"))
        print(f"  {status_icon} [{task_id}] {obj_name}: {old_v} → {new_v}  ({created})")
    print()


def _cmd_version_report_detail(task_id: str, fmt: str = "text") -> None:
    """显示单个版本迭代报告详情."""
    from ila import get_version_report

    report = get_version_report(task_id)

    if not report:
        print(f"报告不存在: {task_id}")
        return

    if fmt == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return

    if fmt == "markdown":
        md = report.get("markdown", "")
        print(md)
        return

    # text format
    print(f"\n{'='*60}")
    print(f"  版本迭代报告: {task_id}")
    print(f"{'='*60}")
    print(f"  旧版本:    {report.get('old_version', '?')}")
    print(f"  新版本:    {report.get('new_version', '?')}")
    print(f"  结论:      {report.get('verdict_label', '?')}")
    print(f"  时间:      {report.get('created_at', '?')}")
    print(f"  耗时:      {report.get('duration_seconds', '?')}s")
    print()

    lifecycle_phases = report.get("lifecycle_phases", [])
    if lifecycle_phases:
        print()
        print("  ╔══════════════════════════════════════════════════╗")
        print("  ║              📋 迭代全流程                       ║")
        print("  ╚══════════════════════════════════════════════════╝")
        for lcp in lifecycle_phases:
            icon = lcp.get("icon", "📍")
            phase = lcp.get("phase", "")
            detail = lcp.get("detail", "")
            phase_conclusion = lcp.get("conclusion", "")
            status = lcp.get("status", "empty")
            status_map = {"done": "✅", "failed": "❌", "skipped": "⏭️", "empty": "⬜"}
            status_mark = status_map.get(status, "⬜")
            status_label = {"done": "已完成", "failed": "失败", "skipped": "已跳过", "empty": "未执行"}.get(status, status)
            print(f"  ┌── {status_mark} {icon} {phase} [{status_label}]")
            if detail:
                print(f"  │   📝 做了什么:")
                # Word-wrap detail at ~70 chars, indented
                import textwrap
                for line in textwrap.wrap(detail, width=68):
                    print(f"  │       {line}")
            if phase_conclusion:
                print(f"  │   📊 结论:")
                import textwrap
                for line in textwrap.wrap(phase_conclusion, width=68):
                    print(f"  │       {line}")
            print(f"  └{'─' * 48}")
        print()

    steps = report.get("steps", [])
    if steps:
        print()
        print("  ── 执行步骤明细 ──")
        for s in steps:
            icon = s.get("icon", "•")
            phase = s.get("phase", "?")
            detail = s.get("detail", "")
            status = s.get("status", "?")
            status_icon = {"success": "✅", "error": "❌", "pending": "⏳"}.get(status, "⬜")
            print(f"    {status_icon} {icon} {phase}: {detail}")
    print()


def cmd_dashboard(args, config, orchestrator):
    """启动可视化管控面板."""
    try:
        from ila.dashboard.api import create_app
        import uvicorn
    except ImportError as e:
        missing = getattr(e, "name", "") or str(e)
        print("Dashboard 依赖未安装或损坏。请运行:")
        print("  pip install fastapi uvicorn")
        if missing:
            print(f"  缺失模块: {missing}  (可尝试: pip install --force-reinstall --no-cache-dir {missing.split('.')[0]})")
        sys.exit(1)

    if getattr(args, "theme", None):
        from ila.dashboard import is_valid_theme, resolve_theme
        if not is_valid_theme(args.theme):
            print(f"⚠️  不支持的主题 '{args.theme}'，将使用默认主题。")
        theme = resolve_theme(args.theme)
        config.setdefault("dashboard", {})["theme"] = theme

    # ── 纳管对象自动刷新配置 ──
    objects_auto_refresh = getattr(args, "objects_auto_refresh", False)
    config.setdefault("dashboard", {})["objects_auto_refresh"] = objects_auto_refresh

    # ── 启动 Launcher (v2) ──
    launcher_started = False
    if not getattr(args, "no_launcher", False):
        try:
            from ila.launcher_manager import start_launcher
            launcher_started = start_launcher()
            if launcher_started:
                print("  Launcher: 已启动 (进程守护)")
        except Exception as e:
            print(f"  ⚠️  Launcher 启动失败: {e} (热升级回退到传统模式)")

    app = create_app(config, sandbox_manager=orchestrator.sandbox_manager, port=args.port)
    print(f"\n{'═' * 60}")
    print(f"  ILA 可视化管控面板启动中...")
    print(f"  访问地址: http://localhost:{args.port}")
    if launcher_started:
        print(f"  热升级: Launcher 模式 (v2)")
    print(f"{'═' * 60}\n")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        # ── 停止 Launcher ──
        if launcher_started:
            try:
                from ila.launcher_manager import stop_launcher
                stop_launcher()
                print("Launcher 已停止")
            except Exception:
                pass


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


# ── 跨平台 CLI 命令 ──────────────────────────────────────────────────

def cmd_trigger(args, config, orchestrator):
    """发起迭代闭环 (通过 API，非阻塞)."""
    import urllib.request
    url = "http://127.0.0.1:9527/api/run"
    payload = json.dumps({
        "object_id": args.object_id,
        "requirement": args.requirement,
        "auto_approve": getattr(args, 'yes', False),
    }).encode()
    try:
        req = urllib.request.Request(url, method="POST", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
    except Exception as e:
        print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=False))
        sys.exit(1)


def cmd_approve(args, config, orchestrator):
    """批准待确认的热切换."""
    import urllib.request
    url = "http://127.0.0.1:9527/api/run/approve"
    try:
        req = urllib.request.Request(url, method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        print(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=False))
        sys.exit(1)


def cmd_skip(args, config, orchestrator):
    """跳过热切换."""
    import urllib.request
    url = "http://127.0.0.1:9527/api/run/skip"
    try:
        req = urllib.request.Request(url, method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        print(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=False))
        sys.exit(1)


def cmd_watch(args, config, orchestrator):
    """查看当前迭代进度 (支持轮询)."""
    import urllib.request
    import time as _time
    url = "http://127.0.0.1:9527/api/run/status"
    while True:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            print(json.dumps(data, ensure_ascii=False))
            status = data.get("status", "")
            if status not in ("running", "pending_approval") or args.once:
                break
            _time.sleep(args.interval)
        except Exception as e:
            print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=False))
            if args.once:
                break
            _time.sleep(args.interval)


def cmd_dashboard_url(args, config, orchestrator):
    """获取 Dashboard 访问地址."""
    import socket
    host = socket.gethostname()
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "127.0.0.1"
    port = config.get("adapters", {}).get("ila", {}).get("dashboard_port", 9527)
    print(json.dumps({"url": f"http://{ip}:{port}"}, ensure_ascii=False))


def cmd_staging_url(args, config, orchestrator):
    """获取 Staging 验证地址."""
    import socket
    host = socket.gethostname()
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "127.0.0.1"
    port = config.get("adapters", {}).get("ila", {}).get("staging_port", 9528)
    print(json.dumps({"url": f"http://{ip}:{port}"}, ensure_ascii=False))


def cmd_list(args, config, orchestrator):
    """列出纳管对象."""
    objects = orchestrator.discover(platform=args.platform)
    if args.json:
        print(json.dumps({"objects": objects, "total": len(objects)}, ensure_ascii=False))
    else:
        if not objects:
            print("未发现纳管对象")
            return
        print(f"\n{'对象ID':<40} {'类型':<10} {'版本':<12} {'平台'}")
        print("-" * 80)
        for obj in objects:
            print(f"{obj['object_id']:<40} {obj['object_type']:<10} "
                  f"{obj['current_version']:<12} {obj.get('platform', '-'):<8}")


def main():
    """CLI 主入口."""
    _ensure_utf8_stdio()
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

    # version (版本历史操作)
    p_version = subparsers.add_parser("version", help="版本历史操作")
    p_version.add_argument("version_id", nargs="?", type=int, help="版本 ID (或使用 --list)")
    p_version.add_argument("--operate", "-o", help="执行操作 (rollback/deploy_verify/stop/iterate)")
    p_version.add_argument("--operations", action="store_true", help="列出按状态映射的可用操作")
    p_version.add_argument("--list", action="store_true", dest="list_versions", help="列出所有版本记录")

    # status
    p_status = subparsers.add_parser("status", help="查看 ILA 状态")
    p_status.add_argument("--verbose", "-v", action="store_true", help="展示各对象当前版本/状态明细")

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="启动可视化管控面板")
    p_dash.add_argument("--host", default="0.0.0.0", help="监听地址")
    p_dash.add_argument("--port", "-p", type=int, default=9527, help="端口")
    p_dash.add_argument("--theme", default=None, help="页面主题 (dark/light/ocean/sepia/grassland/starry)")
    p_dash.add_argument("--page-size", type=int, default=10, help="纳管对象每页显示条数 (默认 10)")
    p_dash.add_argument("--no-launcher", action="store_true", help="禁用 Launcher 进程守护")
    p_dash.add_argument("--objects-auto-refresh", action="store_true", default=False,
                        help="启用纳管对象列表定时轮询刷新 (页面加载默认展示第一页，此开关控制额外定时刷新)")


    # report
    p_report = subparsers.add_parser("report", help="查看迭代报告")
    p_report.add_argument("--version-list", action="store_true", dest="version_report",
                          help="查看 ILA 版本迭代报告列表")
    p_report.add_argument("--version-id", type=str, dest="version_id", default=None,
                          help="查看指定 ILA 版本迭代报告详情")
    p_report.add_argument("--format", choices=["json", "markdown", "text"], default="text",
                          help="输出格式 (默认 text)")

    # trigger (跨平台 CLI 入口)
    p_trigger = subparsers.add_parser("trigger", help="发起迭代闭环 (跨平台 CLI)")
    p_trigger.add_argument("object_id", help="目标对象 ID")
    p_trigger.add_argument("requirement", help="需求描述")
    p_trigger.add_argument("--yes", "-y", action="store_true", help="自动批准")

    # approve / skip (跨平台 CLI)
    subparsers.add_parser("approve", help="批准待确认的热切换")
    subparsers.add_parser("skip", help="跳过热切换")

    # watch (跨平台 CLI)
    p_watch = subparsers.add_parser("watch", help="查看当前迭代进度")
    p_watch.add_argument("--interval", "-i", type=int, default=5, help="轮询间隔秒数 (默认 5)")
    p_watch.add_argument("--once", action="store_true", help="仅查询一次")

    # url (跨平台 CLI)
    subparsers.add_parser("dashboard-url", help="获取 Dashboard 访问地址")
    subparsers.add_parser("staging-url", help="获取 Staging 验证地址")

    # list (跨平台 CLI)
    p_list = subparsers.add_parser("list", help="列出纳管对象 (跨平台 CLI)")
    p_list.add_argument("--platform", "-p", help="按平台过滤")
    p_list.add_argument("--json", action="store_true", help="JSON 格式输出")

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
        "trigger": cmd_trigger,     # trigger 非阻塞，立即返回
        "test": cmd_test,
        "swap": cmd_swap,
        "rollback": cmd_rollback,
        "version": cmd_version,
        "status": cmd_status,
        "dashboard": cmd_dashboard,
        "approve": cmd_approve,
        "skip": cmd_skip,
        "watch": cmd_watch,
        "dashboard-url": cmd_dashboard_url,
        "staging-url": cmd_staging_url,
        "list": cmd_list,
        "report": cmd_report,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args, config, orchestrator)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
