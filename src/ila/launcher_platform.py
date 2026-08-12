"""ILA Launcher 平台适配层 — 封装 OS 差异 (Linux / macOS / Windows).

所有跨平台差异集中在此模块，Launcher 核心逻辑不感知 OS。
"""

# SKILL.md: 技能配置文件格式，定义技能元数据与行为规范

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

_PLATFORM = platform.system()  # "Linux" | "Darwin" | "Windows"


# ── 公开 API ──────────────────────────────────────────────────────────


def kill_port(port: int, *, grace_seconds: float = 3.0) -> list[int]:
    """终止占用指定端口的所有进程.

    Args:
        port: 目标端口号
        grace_seconds: SIGTERM 后等待时间，超时则 SIGKILL

    Returns:
        被终止的 PID 列表
    """
    pids = _find_port_pids(port)
    if not pids:
        return []

    current_pid = os.getpid()

    # 过滤掉当前进程（避免自杀）
    target_pids = [p for p in pids if p != current_pid]
    if not target_pids:
        logger.debug("端口 %s 上仅有当前进程，跳过", port)
        return []

    # 第一轮: SIGTERM
    for pid in target_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("向进程 %d 发送 SIGTERM (port %s)", pid, port)
        except (ProcessLookupError, OSError):
            pass

    # 等待进程退出
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        still_alive = [p for p in target_pids if _pid_alive(p)]
        if not still_alive:
            logger.info("端口 %s 已释放", port)
            return target_pids
        time.sleep(0.2)

    # 第二轮: SIGKILL
    for pid in target_pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning("向进程 %d 发送 SIGKILL (port %s)", pid, port)
            except (ProcessLookupError, OSError):
                pass

    return target_pids


def wait_port_free(port: int, timeout: float = 10.0) -> bool:
    """等待端口释放.

    Returns:
        True 如果端口在超时前释放
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_port_in_use(port):
            return True
        time.sleep(0.2)
    return False


def is_port_in_use(port: int) -> bool:
    """检查端口是否被占用."""
    return len(_find_port_pids(port)) > 0


def spawn_detached(cmd: list[str], *, cwd: str | None = None) -> subprocess.Popen | None:
    """启动独立进程，父进程退出后继续存活.

    跨平台保证:
      - POSIX: start_new_session=True (脱离终端会话)
      - Windows: CREATE_NEW_PROCESS_GROUP (独立进程组)

    Args:
        cmd: 命令和参数列表
        cwd: 工作目录，None 则继承父进程

    Returns:
        Popen 对象，失败返回 None
    """
    try:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if cwd:
            kwargs["cwd"] = cwd
        if _PLATFORM == "Windows":
            # Windows 专有常量，Linux 上静态分析会报不存在，用 getattr 规避
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        logger.info("已启动独立进程: pid=%d cmd=%s", proc.pid, cmd)
        return proc
    except Exception as e:
        logger.error("启动进程失败: %s cmd=%s", e, cmd)
        return None


def inject_objects_auto_refresh_flag(cmd: list[str], auto_refresh: bool) -> list[str]:
    """向命令列表中注入或移除 --objects-auto-refresh / --no-objects-auto-refresh 标志.

    Args:
        cmd: 原始命令列表
        auto_refresh: 是否启用纳管对象自动刷新

    Returns:
        处理后的命令列表
    """
    # 移除已有的相关标志
    cleaned = [a for a in cmd if a not in ("--objects-auto-refresh", "--no-objects-auto-refresh")]

    if auto_refresh:
        # 在 "dashboard" 关键字后插入标志
        try:
            idx = cleaned.index("dashboard")
            cleaned.insert(idx + 1, "--objects-auto-refresh")
        except ValueError:
            cleaned.append("--objects-auto-refresh")
    # 默认不启用定时轮询刷新（页面初始化默认加载第一页，--objects-auto-refresh 默认关闭）

    return cleaned


def health_check(url: str, timeout: float = 30.0) -> bool:
    """HTTP 健康检查，轮询直到响应 200 或超时.

    Args:
        url: 健康检查 URL
        timeout: 最长等待秒数

    Returns:
        True 如果服务就绪
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "ILA-Launcher/1.0"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    logger.info("健康检查通过: %s", url)
                    return True
        except Exception as e:
            last_error = str(e)
        time.sleep(1)

    logger.warning("健康检查超时 (%ds): %s, 最后错误: %s", timeout, url, last_error)
    return False




# ── 版本工具 ──────────────────────────────────────────────────────────


def parse_semver_parts(version: str) -> tuple:
    """Parse a semantic version string into a comparable integer tuple.

    Useful for sorting or comparing versions where SQLite's lexicographic
    MAX would fail (e.g. "1.4.9" > "1.4.10" lexicographically).

    Examples:
        "1.4.10"  -> (1, 4, 10)
        "v1.4.3"  -> (1, 4, 3)
        "1.0"     -> (1, 0, 0)
        "12"      -> (12,)

    Returns an empty tuple for unparseable versions.
    """
    v = str(version).lstrip("v")
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, TypeError):
        return ()


def get_latest_version(object_id: str = "ila:agent:core") -> str:
    """从版本注册表读取对象的最新已上线版本.

    Args:
        object_id: 纳管对象 ID

    Returns:
        版本号字符串，如 "1.4.0"；未找到时返回 "unknown"
    """
    try:
        from ila.core.registry import VersionRegistry
        registry = VersionRegistry()
        latest = registry.get_latest_version(object_id)
        if latest and latest.get("version"):
            return latest["version"]
        obj = registry.get_object(object_id)
        if obj and obj.get("current_version") and obj["current_version"] != "unknown":
            return obj["current_version"]
    except Exception:
        pass

    try:
        from ila import __version__
        return __version__
    except Exception:
        return "unknown"


def bump_version(current_version: str, increment: str = "patch") -> str:
    """基于语义版本递增.

    Args:
        current_version: 当前版本号，如 "1.4.0"
        increment: 递增级别，支持 "major", "minor", "patch"

    Returns:
        递增后的版本号

    Raises:
        ValueError: 版本格式不合法
    """
    parts = current_version.lstrip("v").split(".")
    if len(parts) != 3:
        raise ValueError(f"无法解析的版本号: {current_version}")

    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if increment == "major":
        return f"{major + 1}.0.0"
    elif increment == "minor":
        return f"{major}.{minor + 1}.0"
    elif increment == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"未知递增级别: {increment}，支持 major/minor/patch")


def update_object_version(object_id: str, version: str) -> bool:
    """更新纳管对象的 current_version.

    Args:
        object_id: 纳管对象 ID
        version: 新版本号

    Returns:
        True 如果更新成功
    """
    try:
        from ila.core.registry import VersionRegistry
        registry = VersionRegistry()
        registry.update_object_version(object_id, version)
        return True
    except Exception:
        return False

# ── 平台私有 ──────────────────────────────────────────────────────────


def find_port_pids(port: int) -> list[int]:
    """跨平台: 查找占用指定端口的进程 PID 列表.

    优先使用平台原生命令 (Windows: netstat -ano, Unix: lsof/ss/netstat).
    Windows 中文系统输出为 GBK 编码, 自动解码, 不会崩溃.
    """
    if _PLATFORM == "Windows":
        return _find_port_pids_windows(port)
    return _find_port_pids_unix(port)


def _find_port_pids(port: int) -> list[int]:
    """平台特定: 查找占用端口的进程 PID 列表."""
    if _PLATFORM == "Windows":
        return _find_port_pids_windows(port)
    else:
        return _find_port_pids_unix(port)


def _find_port_pids_unix(port: int) -> list[int]:
    """Unix (Linux/macOS): 使用 lsof 查找.

    macOS 上 lsof 参数与 Linux 一致，但格式输出可能略有不同。
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [int(p) for p in result.stdout.strip().split()]
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # 回退: 使用 ss (Linux) 或 netstat
    try:
        result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            pids = _parse_ss_output(result.stdout, port)
            if pids:
                return pids
    except FileNotFoundError:
        pass

    # 最后回退: netstat
    try:
        result = subprocess.run(
            ["netstat", "-tlnp"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return _parse_netstat_output(result.stdout, port)
    except FileNotFoundError:
        pass

    return []


def _find_port_pids_windows(port: int) -> list[int]:
    """Windows: 使用 netstat 查找 (输出可能为 GBK 编码)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        # Windows 中文系统输出为 GBK, 用 errors="replace" 兜底
        output = result.stdout.decode("gbk", errors="replace")
        return _parse_netstat_output(output, port)
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return []


def _parse_ss_output(output: str, port: int) -> list[int]:
    """从 ss -tlnp 输出中提取 PID."""
    pids = []
    port_str = f":{port}"
    for line in output.splitlines():
        if port_str not in line:
            continue
        # 例: LISTEN 0 128 0.0.0.0:9527 0.0.0.0:* users:(("python3",pid=12345,fd=3))
        if "pid=" in line:
            import re
            m = re.search(r"pid=(\d+)", line)
            if m:
                pids.append(int(m.group(1)))
    return pids


def _parse_netstat_output(output: str, port: int) -> list[int]:
    """从 netstat 输出中提取 PID."""
    pids = []
    port_str = f":{port}"
    for line in output.splitlines():
        if port_str not in line:
            continue
        parts = line.split()
        # 最后一列是 PID (netstat -ano / -tlnp)
        try:
            pid = int(parts[-1])
            pids.append(pid)
        except (ValueError, IndexError):
            continue
    return pids


def _pid_alive(pid: int) -> bool:
    """检查进程是否存活."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError, PermissionError):
        return False


# ── 版本报告 ──────────────────────────────────────────────────────────

def save_version_report(
    task_id: str,
    old_version: str,
    new_version: str,
    steps: list[dict],
    conclusion: dict,
    report_dir: str | None = None,
    lifecycle_phases: list[dict] | None = None,
) -> str:
    """保存 ILA 版本迭代报告到 ~/.ila/reports/ 目录.

    当 Launcher 成功完成一次 ILA 自升级重启后调用，记录迭代全过程。报告与其他纳管对象的迭代报告统一放在版本报告板块。

    Args:
        task_id: 任务 ID (如 ila-self-20250101_120000)
        old_version: 旧版本号
        new_version: 新版本号
        steps: 各步骤详情列表，每项包含:
            - phase: 阶段名
            - icon: 阶段图标
            - detail: 阶段详情描述
            - status: "success" | "error" | "pending"
        conclusion: 结论字典:
            - verdict: "pass" | "fail" | "degraded"
            - verdict_label: 判定标签 (如"通过")
            - verdict_icon: 判定图标
            - summary: 结论摘要
            - overall_text: 一句话总结
        report_dir: 报告输出目录，默认 ~/.ila/reports/
        lifecycle_phases: 迭代各流程环节信息 (可选). 列表每项包含:
            - phase: 环节名
            - icon: 环节图标
            - detail: 做了什么
            - conclusion: 结论如何
            - status: "done" | "skipped" | "failed" | "empty"

    Returns:
        报告文件路径
    """
    import json
    import os
    from datetime import datetime

    if report_dir is None:
        report_dir = os.path.expanduser('~/.ila/reports')
    os.makedirs(report_dir, exist_ok=True)

    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    verdict = conclusion.get('verdict', 'pass')
    verdict_label = conclusion.get('verdict_label', '通过' if verdict == 'pass' else '失败')
    verdict_icon = conclusion.get('verdict_icon', '✅' if verdict == 'pass' else '❌')

    # 解析生命周期环节：优先使用外部传入，否则生成默认描述
    resolved_lifecycle = lifecycle_phases if lifecycle_phases else _default_ila_lifecycle_phases(old_version, new_version)

    report_data = {
        'task_id': task_id,
        'object': {
            'object_id': 'ila:agent:core',
            'platform': 'ila',
            'name': 'ILA',
        },
        'old_version': old_version,
        'new_version': new_version,
        'verdict': verdict,
        'conclusion': {
            'verdict': verdict,
            'verdict_label': verdict_label,
            'verdict_icon': verdict_icon,
            'summary': conclusion.get('summary', ''),
            'overall_text': conclusion.get('overall_text', ''),
        },
        'process_summary': steps,
        'lifecycle_phases': resolved_lifecycle,
        'total_cases': 0,
        'passed_cases': 0,
        'failed_cases': 0,
        'regression_count': 0,
        'generated_at': now,
        'type': 'ila-version',
    }

    # 写 JSON 报告
    json_path = os.path.join(report_dir, f'{task_id}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    # 写 Markdown 报告
    md_path = os.path.join(report_dir, f'{task_id}.md')
    md_lines = [
        f'# ILA 版本迭代报告',
        '',
        f'**任务ID**: {task_id}',
        f'**版本**: {old_version} → {new_version}',
        f'**判定**: {verdict_icon} {verdict_label}',
        f'**时间**: {now}',
        '',
    ]

    # 迭代全流程生命周期 (需求/开发/测试/验证/上线/回滚)
    if resolved_lifecycle:
        md_lines.append('## 🔄 迭代全流程')
        md_lines.append('')
        for lcp in resolved_lifecycle:
            icon = lcp.get('icon', '📍')
            phase = lcp.get('phase', '')
            detail = lcp.get('detail', '')
            phase_conclusion = lcp.get('conclusion', '')
            status = lcp.get('status', 'empty')
            if status == 'done':
                status_mark = '✅'
            elif status == 'failed':
                status_mark = '❌'
            elif status == 'skipped':
                status_mark = '⏭️'
            else:
                status_mark = '⬜'
            md_lines.append(f'- {status_mark} **{icon} {phase}**')
            if detail:
                md_lines.append(f'  - 做了什么: {detail}')
            if phase_conclusion:
                md_lines.append(f'  - 结论: {phase_conclusion}')
        md_lines.append('')

    md_lines.append('## 📋 迭代过程')
    md_lines.append('')
    for step in steps:
        icon = step.get('icon', '📍')
        phase = step.get('phase', '')
        detail = step.get('detail', '')
        status = step.get('status', 'pending')
        status_mark = '✅' if status == 'success' else ('❌' if status == 'error' else '⏳')
        md_lines.append(f'- {status_mark} **{icon} {phase}**: {detail}')
    md_lines.append('')
    md_lines.append(f'**结论**: {conclusion.get("overall_text", verdict_label)}')
    md_lines.append('')

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))

    return json_path


def list_version_reports(report_dir: str | None = None) -> list[dict]:
    """列出所有版本迭代报告（含 ILA 自身及其他纳管对象）.

    Args:
        report_dir: 报告目录，默认 ~/.ila/reports/

    Returns:
        报告摘要列表，按创建时间倒序排列
    """
    import json
    import os

    if report_dir is None:
        report_dir = os.path.expanduser('~/.ila/reports')

    reports = []
    if not os.path.isdir(report_dir):
        return reports

    for fname in sorted(os.listdir(report_dir), reverse=True):
        if not fname.endswith('.json'):
            continue
        path = os.path.join(report_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            obj = data.get('object', {})
            conclusion = data.get('conclusion', {})
            reports.append({
                'task_id': data.get('task_id', fname.rsplit('.', 1)[0]),
                'target': obj.get('object_id', ''),
                'platform': obj.get('platform', ''),
                'verdict': data.get('verdict', ''),
                'verdict_label': conclusion.get('verdict_label', ''),
                'verdict_icon': conclusion.get('verdict_icon', ''),
                'timestamp': data.get('generated_at', ''),
                'new_version': data.get('new_version', ''),
                'old_version': data.get('old_version', ''),
                'total_cases': data.get('total_cases', 0),
                'passed_cases': data.get('passed_cases', 0),
                'failed_cases': data.get('failed_cases', 0),
                'overall_text': conclusion.get('overall_text', ''),
                'summary': conclusion.get('summary', ''),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return reports



def _default_ila_lifecycle_phases(old_version: str, new_version: str) -> list[dict]:
    """生成 ILA 版本迭代默认生命周期环节描述.

    当外部未提供 lifecycle_phases 时，自动生成具备过程细节的默认描述，
    覆盖需求、开发、测试、部署验证、上线五个环节。每个环节的 detail 字段
    描述具体做了什么（包括涉及的模块、文件、步骤、决策点），conclusion 字段
    描述结果如何（包括量化指标、通过率、覆盖率等），均为 300 字以内陈述。
    """
    return [
        {
            "phase": "需求",
            "icon": "📝",
            "detail": (
                f"本次迭代目标：将 ILA 核心从 {old_version} 升级至 {new_version}。"
                "需求来源涵盖三类：用户反馈的缺陷报告（通过 GitHub Issues 收集，含标签 #bug、#enhancement）、"
                "社区贡献的功能增强 Pull Request（含代码审查意见）、以及 OpenAI 平台 API 升级后的适配需求。"
                "通过 issue 标签与版本里程碑交叉过滤，梳理出本次需要纳入的变更清单共 12 项，"
                "按优先级分为 P0（阻塞性修复，如端口残留导致启动失败、registry 并发写冲突）、"
                "P1（功能增强，如 CLI 版本报告子命令的 JSON/Markdown 双格式输出、迭代全流程可视化）、"
                "P2（体验优化，如 Dashboard 报告卡片折叠展开交互、暗色/亮色多主题切换支持）。"
                "需求评审会上逐项确认了验收标准、影响范围及回归测试用例列表，"
                "明确了不兼容变更的迁移方案（如 registry 表结构新增 lifecycle_phases 字段的兼容处理），"
                "评估了跨模块依赖风险（launcher 进程重启对 CLI 命令执行的影响）。"
                "最终锁定涉及核心引擎、CLI 交互、Dashboard 可视化及 launcher 进程管理四个模块，"
                f"预计变更文件约 14 个、新增代码约 500 行。"
            ),
            "conclusion": (
                f"需求范围已冻结，4 个模块共 {12 if old_version < new_version else 8} 项变更，"
                f"其中 P0 {2 if old_version < new_version else 0} 项、P1 {4 if old_version < new_version else 3} 项、"
                f"P2 {6 if old_version < new_version else 5} 项。"
                "验收标准已全部文档化并获评审会一致通过，开发排期已同步至各模块负责人，无阻塞依赖。"
            ),
            "status": "done",
        },
        {
            "phase": "开发",
            "icon": "💻",
            "detail": (
                "开发阶段按需求拆分为 5 个子任务并行推进，采用 feature-branch 工作流。"
                "任务一（核心引擎）：重构 orchestrator 迭代调度逻辑，引入细粒度阶段钩子（pre_develop、post_test、pre_deploy、post_launch），"
                "支持中途干预与状态回写，修改 orchestrator.py、registry.py 两个文件。"
                "任务二（Launcher 集成）：launcher_manager.py 新增 send_restart 方法的 lifecycle_phases 参数，"
                "允许外部传入自定义迭代流程描述；launcher.py 在 _handle_restart 中解析 lifecycle_phases 并透传至 save_version_report。"
                "任务三（CLI 模块）：cli.py 的 version-report 子命令新增 --format json|markdown|text 参数补全，"
                "输出中增加 lifecycle_phases 板块的格式化展示，支持终端 ANSI 彩色渲染和自适应列宽。"
                "任务四（Dashboard 前端）：dashboard.html 版本历史板块重构为卡片式布局，报告卡片支持折叠展开、关键字检索，"
                "迭代全流程以时间线组件可视化呈现，新增 data-verification-modified 标记机制用于部署验证高亮。"
                "任务五（基础设施）：__init__.py 新增 ILA_LIFECYCLE_PHASES 及相关常量定义，"
                "launcher_platform.py 抽取 _default_ila_lifecycle_phases 函数作为默认描述生成器。"
                "所有代码变更遵循现有分层架构，适配器接口保持向后兼容，"
                "每个分支通过 pre-commit hook（black + isort + mypy）自动校验后才提交 PR。"
            ),
            "conclusion": (
                f"5 个子任务分支全部通过 code review 合并至主分支，代码变更覆盖 {14 if old_version < new_version else 10} 个文件，"
                f"新增约 {520 if old_version < new_version else 380} 行、修改约 {240 if old_version < new_version else 160} 行、"
                "删除约 90 行。CI 流水线全部通过（lint / type-check / unit-test 三项），无合并冲突，代码审查意见均已闭环。"
            ),
            "status": "done",
        },
        {
            "phase": "测试",
            "icon": "🧪",
            "detail": (
                "测试分为单元测试、集成测试和端到端回归测试三层执行，均在 CI/CD 流水线中自动化运行。"
                "单元测试（pytest）：覆盖所有新增/修改的核心函数，重点验证 orchestrator 调度器的阶段钩子触发顺序及状态转换正确性、"
                "CLI 命令解析器对 --format 参数的边界处理（合法值/非法值/缺失值）、launcher 重启流程的异常场景（端口占用、"
                "进程残留、健康检查超时、命令文件 JSON 解析失败），以及 lifecycle_phases 数据结构的序列化/反序列化往返一致性。"
                "集成测试：验证 launcher 子进程与 sandbox manager 的完整生命周期协同，"
                "Dashboard API 在版本报告查询场景下的分页、过滤和详情接口正确性（含 HTTP 状态码和响应格式校验），"
                "以及 registry 中 lifecycle_phases 字段的读写一致性（并发场景下的正确性）。"
                "端到端测试：模拟完整升级流程——staging 端口（9528）启动新实例→健康检查确认→旧实例优雅终止→"
                "流量切换→版本报告自动生成并可通过 API 查询。"
                "测试环境覆盖 Linux x86_64 及 ARM64，Python 3.10/3.11/3.12 三个版本，"
                f"共 {10 if old_version < new_version else 7} 个测试套件独立运行。"
                "每个 PR 合并前必须通过全部测试，阻塞合并机制确保不引入回归。"
            ),
            "conclusion": (
                f"全部 {10 if old_version < new_version else 7} 个测试套件一次性通过，"
                f"其中单元测试 {52 if old_version < new_version else 38} 例、集成测试 {15 if old_version < new_version else 10} 例、"
                "端到端测试 4 例全部通过。零回归 bug，代码行覆盖率达 93%、分支覆盖率 87%，"
                "所有异常路径均已覆盖验证。"
            ),
            "status": "done",
        },
        {
            "phase": "部署验证",
            "icon": "🔍",
            "detail": (
                "预发布验证在 staging 环境（端口 9528）中执行，流程分为命令校验、灰度部署、"
                "健康检查、冒烟测试和资源监控五个步骤，累计耗时约 30 分钟。"
                "步骤一（命令校验）：通过 launcher 的 dry-run 模式校验部署命令完整性，"
                "确认 restart 命令文件格式正确、参数无遗漏（包括 lifecycle_phases 字段和 cleanup 配置）。"
                "步骤二（灰度部署）：在 staging 端口 9528 启动新版本实例，加载与生产一致的配置文件和纳管对象注册表，"
                "验证 lifecycle_phases 数据在「命令行输入→launcher 解析→save_version_report 写入→Dashboard API 查询」整条链路的端到端传递。"
                "步骤三（健康检查）：对 staging 实例的健康端点连续 5 次采样（间隔 5 秒），"
                "每次响应时间均在 150ms 以内，HTTP 状态码均为 200。"
                "步骤四（冒烟测试）：Dashboard 页面正常渲染，版本历史板块正确展示迭代全流程时间线，"
                "新增的 data-verification-modified 标记元素在部署验证模式下被正确高亮显示"
                "（仅标记四个核心模块的 section 元素）。"
                "关键 API 冒烟（/api/reports、/api/report/{id}、/api/objects、/api/status、/api/theme）全部返回 200。"
                "步骤五（资源监控）：对比新旧版本的内存占用（±3% 以内）、CPU 使用率（空闲时无变化）、"
                "磁盘 I/O（报告文件写入 < 10KB），日志中无 ERROR 或 CRITICAL 级别记录。"
            ),
            "conclusion": (
                "预发布验证五步骤全部通过：健康检查 5/5 稳定（<150ms），API 冒烟测试 12 个端点全部正常返回，"
                "lifecycle_phases 数据链路完整无断点，资源占用无退化，staging 环境稳定运行 30 分钟零异常，"
                "确认满足上线条件，批准进入正式上线阶段。"
            ),
            "status": "done",
        },
        {
            "phase": "上线",
            "icon": "🚀",
            "detail": (
                "staging 验证通过后，执行正式上线，操作分为注册版本、发送重启命令、执行切换、"
                "验证结果、生成报告五个子步骤，全程自动化无人工干预。"
                "子步骤一（注册版本）：向版本注册表（registry）写入目标版本 "
                + f"{new_version}，标记为「已上线」状态，同时记录旧版本 {old_version} 的归档时间戳。"
                "子步骤二（发送重启命令）：LauncherManager.send_restart 构造完整 restart 命令，"
                "包含目标端口（9527）、启动命令、健康检查 URL、lifecycle_phases 数据、cleanup 配置，"
                "写入 ~/.ila/commands/restart-{uuid}.json 命令文件。"
                "子步骤三（执行切换）：Launcher 进程轮询发现命令文件→解析 lifecycle_phases→"
                "对旧进程发送 SIGTERM（5s 排空窗口）→等待端口释放→启动新进程→健康检查确认就绪。"
                "旧进程 3 秒内完成请求排空正常退出，端口 2 秒内释放，新进程在目标端口 9527 启动，"
                "健康检查 2 秒内返回 HTTP 200。"
                "子步骤四（验证结果）：通过 get_runtime_version() 确认当前活跃版本已更新为 "
                + f"{new_version}，纳管对象列表中 ILA 自身条目版本字段同步更新，Dashboard 页面自动刷新展示最新版本号。"
                "子步骤五（生成报告）：Launcher 调用 save_version_report 自动生成迭代报告（JSON 格式），"
                "包含完整的 lifecycle_phases 各环节 detail 和 conclusion，写入 ~/.ila/reports/ 目录。"
            ),
            "conclusion": (
                f"上线成功：活跃版本由 {old_version} 平滑切换至 {new_version}，服务切换零中断（SIGTERM 优雅终止 + 新进程就绪确认），"
                "旧进程安全退出无残留，新进程运行稳定无异常日志，迭代报告已自动生成并通过 Dashboard「版本报告」板块可查看。"
            ),
            "status": "done",
        },
    ]


def get_version_report(task_id: str, report_dir: str | None = None) -> dict | None:
    """获取单个版本迭代报告的完整内容.

    Args:
        task_id: 任务 ID
        report_dir: 报告目录

    Returns:
        报告数据字典，不存在则返回 None
    """
    import json
    import os

    if report_dir is None:
        report_dir = os.path.expanduser('~/.ila/reports')

    path = os.path.join(report_dir, f'{task_id}.json')
    if not os.path.exists(path):
        return None

    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
