"""ILA: Iteration Loop Agent - 平台无关的敏捷迭代闭环智能体."""

# SKILL.md: 技能配置文件格式，定义技能元数据与行为规范
from __future__ import annotations

__version__ = "1.5.0"

# 运行时版本: 优先从注册表读取最新已上线版本，fallback 到静态 __version__
_runtime_version: str | None = None


def get_runtime_version(refresh: bool = False) -> str:
    """获取运行时版本号.


    优先从版本注册表 (SQLite) 读取 ila:agent:core 的最新已上线版本；
    如果注册表不可用或没有记录，则 fallback 到 __version__。

    Args:
        refresh: 如果为 True，强制重新读取注册表

    Returns:
        版本字符串，例如 "1.4.0"
    """
    global _runtime_version
    if not refresh and _runtime_version is not None:
        return _runtime_version

    try:
        from ila.core.registry import VersionRegistry
        registry = VersionRegistry()
        obj = registry.get_object("ila:agent:core")
        if obj and obj.get("current_version") and obj["current_version"] != "unknown":
            _runtime_version = obj["current_version"]
            return _runtime_version
    except Exception:
        pass

    _runtime_version = __version__
    return _runtime_version


def set_runtime_version(version: str) -> None:
    """设置运行时版本号 (供 Launcher 部署成功后回写)."""
    global _runtime_version
    _runtime_version = version
    try:
        from ila.core.registry import VersionRegistry
        registry = VersionRegistry()
        registry.update_object_version("ila:agent:core", version)
    except Exception:
        pass



# Dashboard 页面主题目录 (暗色 / 亮色 / 其他风格)，由 dashboard 子包引用
DASHBOARD_THEMES: tuple[str, ...] = ("dark", "light", "ocean", "sepia", "grassland", "starry")
DEFAULT_DASHBOARD_THEME: str = "dark"

# 纳管对象列表周期性自动刷新 (v1.4) — 页面加载时默认展示第一页，此开关控制是否定时轮询刷新
DASHBOARD_OBJECTS_AUTO_REFRESH: bool = False
DASHBOARD_OBJECTS_REFRESH_INTERVAL: int = 15   # 自动刷新间隔（秒），仅当 DASHBOARD_OBJECTS_AUTO_REFRESH=True 时生效

# 版本迭代全流程环节定义
# 五个标准环节：需求 → 开发 → 测试 → 部署验证 → 上线
ILA_LIFECYCLE_PHASES: tuple[str, ...] = ("需求", "开发", "测试", "部署验证", "上线")
ILA_LIFECYCLE_PHASE_ICONS: dict[str, str] = {
    "需求": "📝",
    "开发": "💻",
    "测试": "🧪",
    "部署验证": "🔍",
    "上线": "🚀",
}
ILA_LIFECYCLE_PHASE_STATUSES: tuple[str, ...] = ("done", "failed", "skipped", "empty")
DEFAULT_LIFECYCLE_PHASE_STATUS: str = "done"



# 版本迭代报告存储目录
# Launcher 执行重启后自动生成报告，dashboard 通过 get_version_reports() / get_version_report() 查询
import os as _os
VERSION_REPORT_DIR: str = _os.path.join(_os.path.expanduser("~"), ".ila", "reports")
VERSION_REPORT_PREFIX: str = "version-iteration"

def parse_semver_parts(version: str) -> tuple:
    """Parse a semantic version string into a comparable integer tuple.

    See ila.launcher_platform.parse_semver_parts for details.
    """
    from ila.launcher_platform import parse_semver_parts as _parse
    return _parse(version)


def list_version_reports(report_dir: str | None = None) -> list[dict]:
    """列出所有版本迭代报告（含 ILA 自身及其他纳管对象）.

    Args:
        report_dir: 报告目录，默认 ~/.ila/reports/

    Returns:
        报告摘要列表，每个包含 task_id, old_version, new_version, verdict,
        created_at, steps 等字段
    """
    from ila.launcher_platform import list_version_reports
    return list_version_reports(report_dir)


def get_version_report(task_id: str, report_dir: str | None = None) -> dict | None:
    """获取单个版本迭代报告.

    Args:
        task_id: 报告 task_id
        report_dir: 报告目录

    Returns:
        完整报告数据，不存在则返回 None
    """
    from ila.launcher_platform import get_version_report
    return get_version_report(task_id, report_dir)


# # 版本历史可用操作 (按版本状态映射)
# live(已上线) -> rollback(回滚)
# testing(测试中) -> deploy_verify(部署验证)
# developing(开发中) -> stop(停止)
# failed(失败) -> iterate(重新迭代)
VERSION_OPERATIONS: dict[str, tuple[str, ...]] = {
    "live": ("rollback",),
    "testing": ("deploy_verify",),
    "developing": ("stop",),
    "failed": ("iterate",),
}

# 操作元数据: 名称 -> {label(中文标签), target_status(目标状态)}
_VERSION_OPERATION_META: dict[str, dict[str, str]] = {
    "rollback": {"label": "回滚", "target_status": "rolled_back"},
    "deploy_verify": {"label": "部署验证", "target_status": "verified"},
    "stop": {"label": "停止", "target_status": "stopped"},
    "iterate": {"label": "重新迭代", "target_status": "developing"},
}


def get_version_operations(status: str) -> list[str]:
    """返回给定版本状态下可用的操作列表."""
    return list(VERSION_OPERATIONS.get(status, ()))


def is_valid_version_operation(operation: str) -> bool:
    """判断给定操作是否受支持."""
    return operation in _VERSION_OPERATION_META


def get_operation_label(operation: str) -> str:
    """返回操作的中文展示标签."""
    return _VERSION_OPERATION_META.get(operation, {}).get("label", operation)


def get_operation_target_status(operation: str) -> str:
    """返回操作执行后的目标版本状态."""
    return _VERSION_OPERATION_META.get(operation, {}).get("target_status", "")


def get_dashboard_objects_auto_refresh() -> bool:
    """返回纳管对象列表是否启用自动刷新."""
    return DASHBOARD_OBJECTS_AUTO_REFRESH


__all__ = [
    "__version__",
    "get_runtime_version",
    "set_runtime_version",
    "DASHBOARD_THEMES",
    "DEFAULT_DASHBOARD_THEME",
    "DASHBOARD_OBJECTS_AUTO_REFRESH",
    "DASHBOARD_OBJECTS_REFRESH_INTERVAL",
    "VERSION_OPERATIONS",
    "get_version_operations",
    "is_valid_version_operation",
    "get_operation_label",
    "get_operation_target_status",
    "VERSION_REPORT_DIR",
    "VERSION_REPORT_PREFIX",
    "list_version_reports",
    "get_version_report",
    "get_dashboard_objects_auto_refresh",
    "ILA_LIFECYCLE_PHASES",
    "ILA_LIFECYCLE_PHASE_ICONS",
    "ILA_LIFECYCLE_PHASE_STATUSES",
    "DEFAULT_LIFECYCLE_PHASE_STATUS",
]
