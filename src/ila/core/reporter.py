"""Reporter: ILA 迭代报告生成器 - 支持 JSON / Markdown / HTML 三种格式输出."""

from __future__ import annotations

import json
import os
from datetime import datetime
from html import escape
from typing import Any

from ila.models.managed_object import ManagedObject
from ila.models.task_spec import TaskSpec


# ---- Verdict 辅助 ----

_VERDICT_ICON = {
    "pass": "✅",
    "fail": "❌",
    "degraded": "⚠️",
    "pending": "⏳",
}

_VERDICT_LABEL_CN = {
    "pass": "通过",
    "fail": "失败",
    "degraded": "降级",
    "pending": "待定",
}


def _verdict_icon(verdict: str) -> str:
    """获取判定图标."""
    return _VERDICT_ICON.get(verdict, "❓")


def _verdict_label(verdict: str) -> str:
    """获取判定中文标签."""
    return _VERDICT_LABEL_CN.get(verdict, verdict)


def _pass_icon(passed: bool) -> str:
    """通过/失败图标."""
    return "✅ PASS" if passed else "❌ FAIL"


class Reporter:
    """ILA 迭代报告生成器.

    生成三种格式的迭代报告:
    - JSON: 完整结构化数据
    - Markdown: 适合在聊天/终端中展示的摘要
    - HTML: 带简单样式的可视化报告 (内联CSS, 无外部依赖)
    """

    # ---- 公共接口 ----

    def generate(
        self,
        obj: ManagedObject,
        task_spec: TaskSpec,
        test_results: dict[str, Any],
        deploy_result: dict[str, Any],
        swap_result: dict[str, Any],
    ) -> dict[str, str]:
        """生成多格式报告.

        Args:
            obj: 被纳管的目标对象
            task_spec: 迭代任务规格书
            test_results: A/B 测试结果 (TestResult.to_dict() 格式)
            deploy_result: 部署验证结果
            swap_result: 热切换结果 (含回滚点路径、新版本等)

        Returns:
            ``{"json": ..., "markdown": ..., "html": ...}``
        """
        report_data = self._build_report_data(
            obj, task_spec, test_results, deploy_result, swap_result
        )
        return {
            "json": self._generate_json(report_data),
            "markdown": self._generate_markdown(report_data),
            "html": self._generate_html(report_data),
        }

    def save_report(
        self, report: dict[str, str], output_dir: str, task_id: str
    ) -> dict[str, str]:
        """保存报告到文件.

        Args:
            report: ``generate()`` 返回的报告字典
            output_dir: 输出目录
            task_id: 任务 ID (用于文件命名)

        Returns:
            各格式文件路径 ``{"json": path, "markdown": path, "html": path}``
        """
        os.makedirs(output_dir, exist_ok=True)
        paths: dict[str, str] = {}
        ext_map = {"json": "json", "markdown": "md", "html": "html"}
        for fmt, ext in ext_map.items():
            path = os.path.join(output_dir, f"{task_id}.{ext}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(report[fmt])
            paths[fmt] = path
        return paths

    # ---- 数据构建 ----

    def _build_report_data(
        self,
        obj: ManagedObject,
        task_spec: TaskSpec,
        test_results: dict[str, Any],
        deploy_result: dict[str, Any],
        swap_result: dict[str, Any],
    ) -> dict[str, Any]:
        """构建统一的报告数据结构."""
        new_version = swap_result.get("new_version", "unknown")
        rollback_point = swap_result.get("rollback_snapshot", "")
        verdict = test_results.get("verdict", "pending")
        case_results = test_results.get("case_results", [])

        # 统计变更类型
        change_type_counts: dict[str, int] = {}
        for change in task_spec.changes:
            ct = change.change_type
            change_type_counts[ct] = change_type_counts.get(ct, 0) + 1

        return {
            "task_id": task_spec.task_id,
            "generated_at": datetime.now().isoformat(),
            "object": obj.to_dict(),
            "task_spec": task_spec.to_dict(),
            "test_results": test_results,
            "deploy_result": deploy_result,
            "swap_result": swap_result,
            # 便捷字段
            "verdict": verdict,
            "new_version": new_version,
            "rollback_point": rollback_point,
            "current_version": obj.current_version,
            "platform": obj.platform,
            "object_id": obj.object_id,
            "change_count": len(task_spec.changes),
            "change_type_counts": change_type_counts,
            "case_results": case_results,
            "total_cases": test_results.get("total_cases", len(case_results)),
            "passed_cases": test_results.get("passed_cases", 0),
            "failed_cases": test_results.get("failed_cases", 0),
            "regression_count": test_results.get("regression_count", 0),
            "summary": test_results.get("summary", ""),
            # 迭代过程 & 结论
            "process_summary": self._build_process_summary(task_spec, test_results, deploy_result, swap_result),
            "conclusion": self._build_conclusion(task_spec, test_results, deploy_result, swap_result, verdict),
            "requirement": task_spec.requirement,
            "sandbox_level": task_spec.sandbox_level,
            "rollback_plan": task_spec.rollback_plan,
        }

    # ---- 迭代过程 & 结论构建 ----

    def _build_process_summary(
        self,
        task_spec: TaskSpec,
        test_results: dict[str, Any],
        deploy_result: dict[str, Any],
        swap_result: dict[str, Any],
    ) -> list[dict[str, str]]:
        """构建迭代过程摘要 - 描述每个阶段做了什么."""
        phases: list[dict[str, str]] = []

        # 阶段1: 需求分析
        phases.append({
            "phase": "需求分析",
            "icon": "🔍",
            "detail": f"分析需求: {task_spec.requirement}",
        })

        # 阶段2: 沙箱开发
        changes = task_spec.changes
        if changes:
            change_descs = []
            for c in changes:
                ct_label = self._change_type_label(c.change_type)
                change_descs.append(f"{ct_label}: {c.description}")
            phases.append({
                "phase": "沙箱开发",
                "icon": "🔧",
                "detail": "变更项:\n" + "\n".join(f"  - {d}" for d in change_descs),
            })
        else:
            phases.append({
                "phase": "沙箱开发",
                "icon": "🔧",
                "detail": "无变更项",
            })

        # 阶段3: A/B 测试
        total = test_results.get("total_cases", 0)
        passed = test_results.get("passed_cases", 0)
        failed = test_results.get("failed_cases", 0)
        regression = test_results.get("regression_count", 0)
        if total > 0:
            detail = f"执行 {total} 个测试用例, {passed} 通过, {failed} 失败"
            if regression > 0:
                detail += f", {regression} 回归"
            phases.append({
                "phase": "A/B 对比测试",
                "icon": "🧪",
                "detail": detail,
            })
        else:
            phases.append({
                "phase": "A/B 对比测试",
                "icon": "🧪",
                "detail": "无测试用例",
            })

        # 阶段4: 部署验证
        deploy_passed = deploy_result.get("passed", False)
        if deploy_passed:
            phases.append({
                "phase": "部署验证",
                "icon": "🚀",
                "detail": "部署验证通过, 新版本已部署至沙箱环境",
            })
        else:
            issues = deploy_result.get("issues", [])
            detail = "部署验证未通过"
            if issues:
                detail += ": " + "; ".join(str(i) for i in issues)
            phases.append({
                "phase": "部署验证",
                "icon": "🚀",
                "detail": detail,
            })

        # 阶段5: 热切换上线
        swap_status = swap_result.get("status", "unknown")
        if swap_status == "success":
            phases.append({
                "phase": "热切换上线",
                "icon": "🔄",
                "detail": "新版本已成功切换上线, 服务正常运行",
            })
        elif swap_status == "rolled_back":
            reason = swap_result.get("reason", "未知原因")
            phases.append({
                "phase": "热切换上线",
                "icon": "⏪",
                "detail": f"热切换失败, 已自动回滚至上一版本 (原因: {reason})",
            })
        else:
            phases.append({
                "phase": "热切换上线",
                "icon": "⏳",
                "detail": f"热切换状态: {swap_status}",
            })

        return phases

    def _build_conclusion(
        self,
        task_spec: TaskSpec,
        test_results: dict[str, Any],
        deploy_result: dict[str, Any],
        swap_result: dict[str, Any],
        verdict: str,
    ) -> dict[str, str]:
        """构建最终结论."""
        swap_status = swap_result.get("status", "unknown")
        new_version = swap_result.get("new_version", "unknown")
        deploy_passed = deploy_result.get("passed", False)
        total = test_results.get("total_cases", 0)
        passed = test_results.get("passed_cases", 0)
        regression = test_results.get("regression_count", 0)

        # 综合判定
        if swap_status == "success":
            overall = "pass"
            overall_text = "✅ 迭代成功, 新版本已上线运行"
        elif swap_status == "rolled_back":
            overall = "rolled_back"
            overall_text = "⏪ 迭代已回滚, 新版本因异常自动下线, 服务恢复至迭代前状态"
        elif deploy_passed:
            overall = "deployed"
            overall_text = "⚠️ 新版本已部署但尚未完成热切换, 需人工介入"
        else:
            overall = "failed"
            overall_text = "❌ 迭代失败, 部署验证未通过, 未上线"

        # 测试总结
        if total > 0:
            test_summary = f"共 {total} 个测试用例, {passed} 通过"
            if regression > 0:
                test_summary += f", 检测到 {regression} 个回归问题"
        else:
            test_summary = "未执行测试用例"

        # 版本信息
        version_info = f"目标对象版本: v{task_spec.target_object_id.split(':')[-1] if ':' in task_spec.target_object_id else task_spec.target_object_id} -> v{new_version}"

        # 生成结论摘要
        if overall == "pass":
            conclusion_summary = f"本次迭代已完成全部六阶段闭环流程, 变更已通过 A/B 测试和部署验证, 新版本成功切换上线。"
        elif overall == "rolled_back":
            conclusion_summary = f"本次迭代已完成需求分析、沙箱开发和测试阶段, 但热切换过程中出现异常, 系统已自动回滚至上一版本, 建议排查原因后重新发起迭代。"
        elif overall == "deployed":
            conclusion_summary = f"本次迭代已完成部署验证, 新版本已部署至沙箱环境, 但热切换阶段尚未完成, 建议人工检查后继续。"
        else:
            conclusion_summary = f"本次迭代未能通过部署验证阶段, 变更未上线, 服务保持原有版本运行。"

        return {
            "overall": overall,
            "overall_text": overall_text,
            "test_summary": test_summary,
            "version_info": version_info,
            "summary": conclusion_summary,
            "verdict": verdict,
            "verdict_label": _verdict_label(verdict),
            "verdict_icon": _verdict_icon(verdict),
        }

    @staticmethod
    def _change_type_label(change_type: str) -> str:
        """变更类型中文标签."""
        labels = {
            "bugfix": "Bug修复",
            "feature": "新功能",
            "refactor": "重构",
            "optimization": "优化",
        }
        return labels.get(change_type, change_type)

    # ---- JSON ----

    def _generate_json(self, data: dict[str, Any]) -> str:
        """生成 JSON 格式报告."""
        return json.dumps(data, ensure_ascii=False, indent=2)

    # ---- Markdown ----

    def _generate_markdown(self, data: dict[str, Any]) -> str:
        """生成 Markdown 格式报告."""
        lines: list[str] = []
        task_id = data["task_id"]
        obj = data["object"]
        task_spec = data["task_spec"]
        verdict = data["verdict"]

        # 标题
        lines.append(f"# ILA 迭代报告 #{task_id}")
        lines.append("")

        # 基本信息
        current_ver = data["current_version"]
        new_ver = data["new_version"]
        lines.append(f"📦 **目标**: `{obj['object_id']}` (v{current_ver} -> v{new_ver})")

        change_count = data["change_count"]
        change_type_counts = data["change_type_counts"]
        if change_type_counts:
            parts = [f"{cnt} {ct}" for ct, cnt in change_type_counts.items()]
            change_summary = " + ".join(parts)
        else:
            change_summary = ""
        lines.append(f"📋 **变更**: {change_count} 项 ({change_summary})" if change_summary
                     else f"📋 **变更**: {change_count} 项")
        lines.append(f"🔧 **平台**: {data['platform']}")
        lines.append("")

        # 需求描述
        requirement = task_spec.get("requirement", "")
        if requirement:
            lines.append(f"📝 **需求**: {requirement}")
            lines.append("")

        # 变更清单
        changes = task_spec.get("changes", [])
        if changes:
            lines.append("## 变更清单")
            lines.append("")
            for i, change in enumerate(changes, 1):
                ct = change.get("change_type", "unknown")
                desc = change.get("description", "")
                complexity = change.get("estimated_complexity", "")
                lines.append(f"{i}. **[{ct}]** {desc} (复杂度: {complexity})")
            lines.append("")

        # A/B 测试结果
        lines.append("## A/B 测试结果")
        lines.append("")
        case_results = data["case_results"]
        if case_results:
            lines.append("| 测试项 | 类型 | A1 (旧版) | A2 (新版) | 状态 |")
            lines.append("|--------|------|-----------|-----------|------|")
            for case in case_results:
                case_id = case.get("case_id", "")
                test_type = case.get("test_type", "")
                a1_pass = case.get("a1_pass", False)
                a2_pass = case.get("a2_pass", False)
                status = "PASS" if a2_pass else "FAIL"
                lines.append(
                    f"| {case_id} | {test_type} | {_pass_icon(a1_pass)} | "
                    f"{_pass_icon(a2_pass)} | {status} |"
                )
            lines.append("")

            # 统计
            total = data["total_cases"]
            passed = data["passed_cases"]
            failed = data["failed_cases"]
            regression = data["regression_count"]
            lines.append(
                f"**统计**: {passed}/{total} 通过, {failed} 失败"
                + (f", {regression} 回归" if regression > 0 else "")
            )
            lines.append("")
        else:
            lines.append("_无测试用例_")
            lines.append("")

        # 判定
        icon = _verdict_icon(verdict)
        label = _verdict_label(verdict)
        lines.append(f"**判定**: {icon} {label}")
        lines.append("")

        # 迭代过程
        lines.append("## 📋 迭代过程")
        lines.append("")
        process_phases = data.get("process_summary", [])
        for phase in process_phases:
            ph_icon = phase.get("icon", "")
            ph = phase.get("phase", "")
            detail = phase.get("detail", "")
            lines.append(f"- **{ph_icon} {ph}**: {detail}")
        lines.append("")

        # 测试统计
        total = data["total_cases"]
        passed = data["passed_cases"]
        failed = data["failed_cases"]
        regression = data["regression_count"]
        if total > 0:
            lines.append(f"**测试统计**: {passed}/{total} 通过, {failed} 失败"
                         + (f", {regression} 回归" if regression > 0 else ""))
            lines.append("")

        # 结论
        lines.append("## 📊 结论")
        lines.append("")
        conclusion = data.get("conclusion", {})
        overall_text = conclusion.get("overall_text", "")
        if overall_text:
            lines.append(overall_text)
            lines.append("")

        conclusion_summary = conclusion.get("summary", "")
        if conclusion_summary:
            lines.append(conclusion_summary)
            lines.append("")

        version_info = conclusion.get("version_info", "")
        if version_info:
            lines.append(f"**版本**: {version_info}")
            lines.append("")

        test_conclusion = conclusion.get("test_summary", "")
        if test_conclusion:
            lines.append(f"**测试**: {test_conclusion}")
            lines.append("")

        # 回滚点
        rollback = data["rollback_point"]
        if rollback:
            lines.append(f"**回滚点**: `{rollback}`")
            lines.append("")

        # 部署信息
        deploy = data["deploy_result"]
        if deploy:
            lines.append("## 部署信息")
            lines.append("")
            for key, value in deploy.items():
                lines.append(f"- **{key}**: {value}")
            lines.append("")

        # 补充说明
        summary = data["summary"]
        if summary:
            lines.append("## 📝 补充说明")
            lines.append("")
            lines.append(summary)
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    # ---- HTML ----

    def _generate_html(self, data: dict[str, Any]) -> str:
        """生成自包含 HTML 报告 (内联 CSS)."""
        task_id = data["task_id"]
        obj = data["object"]
        task_spec = data["task_spec"]
        verdict = data["verdict"]
        generated_at = data["generated_at"]

        verdict_class = {
            "pass": "verdict-pass",
            "fail": "verdict-fail",
            "degraded": "verdict-degraded",
            "pending": "verdict-pending",
        }.get(verdict, "verdict-pending")

        icon = _verdict_icon(verdict)
        label = _verdict_label(verdict)

        # 基本信息行
        current_ver = data["current_version"]
        new_ver = data["new_version"]
        change_count = data["change_count"]
        change_type_counts = data["change_type_counts"]
        if change_type_counts:
            change_summary = " + ".join(
                f"{cnt} {ct}" for ct, cnt in change_type_counts.items()
            )
        else:
            change_summary = ""

        # 变更清单
        changes = task_spec.get("changes", [])
        changes_rows = ""
        for i, change in enumerate(changes, 1):
            changes_rows += (
                f"<tr>"
                f"<td>{i}</td>"
                f"<td><span class='badge badge-{escape(change.get('change_type', ''))}'>"
                f"{escape(change.get('change_type', ''))}</span></td>"
                f"<td>{escape(change.get('description', ''))}</td>"
                f"<td>{escape(change.get('estimated_complexity', ''))}</td>"
                f"</tr>"
            )
        changes_section = ""
        if changes_rows:
            changes_section = f"""
        <section>
            <h2>📋 变更清单</h2>
            <table class="data-table">
                <thead><tr><th>#</th><th>类型</th><th>描述</th><th>复杂度</th></tr></thead>
                <tbody>{changes_rows}</tbody>
            </table>
        </section>"""

        # A/B 测试表
        case_results = data["case_results"]
        test_rows = ""
        for case in case_results:
            case_id = escape(str(case.get("case_id", "")))
            test_type = escape(str(case.get("test_type", "")))
            a1_pass = case.get("a1_pass", False)
            a2_pass = case.get("a2_pass", False)
            a1_cell = f"<td class='{'pass' if a1_pass else 'fail'}'>{'✅ PASS' if a1_pass else '❌ FAIL'}</td>"
            a2_cell = f"<td class='{'pass' if a2_pass else 'fail'}'>{'✅ PASS' if a2_pass else '❌ FAIL'}</td>"
            status_cls = "pass" if a2_pass else "fail"
            status_text = "PASS" if a2_pass else "FAIL"
            test_rows += (
                f"<tr>"
                f"<td>{case_id}</td>"
                f"<td>{test_type}</td>"
                f"{a1_cell}{a2_cell}"
                f"<td class='{status_cls}'>{status_text}</td>"
                f"</tr>"
            )

        test_section = ""
        if case_results:
            total = data["total_cases"]
            passed = data["passed_cases"]
            failed = data["failed_cases"]
            regression = data["regression_count"]
            test_section = f"""
        <section>
            <h2>🧪 A/B 测试结果</h2>
            <table class="data-table">
                <thead>
                    <tr><th>测试项</th><th>类型</th><th>A1 (旧版)</th><th>A2 (新版)</th><th>状态</th></tr>
                </thead>
                <tbody>{test_rows}</tbody>
            </table>
            <div class="stats">
                <span class="stat-item">通过: <strong>{passed}/{total}</strong></span>
                <span class="stat-item">失败: <strong>{failed}</strong></span>
                {"<span class='stat-item stat-regression'>回归: <strong>" + str(regression) + "</strong></span>" if regression > 0 else ""}
            </div>
        </section>"""
        else:
            test_section = """
        <section>
            <h2>🧪 A/B 测试结果</h2>
            <p class="muted">无测试用例</p>
        </section>"""

        # 回滚点
        rollback = data["rollback_point"]
        rollback_section = ""
        rollback_css = ""
        if rollback:
            rollback_css = """
        .rollback-bar {
            background: #e3f2fd; border-radius: 8px; padding: 14px 20px;
            margin-bottom: 20px; display: flex; align-items: center; gap: 12px;
        }
        .rollback-bar code {
            background: #fff; padding: 4px 12px; border-radius: 4px;
            font-size: 0.85rem; color: #1565c0; border: 1px solid #bbdefb;
        }"""
            rollback_section = f"""
        <div class="rollback-bar">
            <strong>🔙 回滚点</strong>
            <code>{escape(rollback)}</code>
        </div>"""

        # 迭代过程
        process_phases = data.get("process_summary", [])
        process_section = ""
        if process_phases:
            phase_items = "".join(
                f"<li style='padding:6px 0;'><strong>{escape(p.get('icon',''))} {escape(p.get('phase',''))}</strong>: {escape(p.get('detail',''))}</li>"
                for p in process_phases
            )
            process_section = f"""
        <section>
            <h2>📋 迭代过程</h2>
            <ul style="list-style:none;padding-left:0;">{phase_items}</ul>
        </section>"""

        # 结论
        conclusion = data.get("conclusion", {})
        conclusion_section = ""
        if conclusion:
            overall_text = conclusion.get("overall_text", "")
            conclusion_summary = conclusion.get("summary", "")
            version_info = conclusion.get("version_info", "")
            test_summary = conclusion.get("test_summary", "")
            conclusion_parts = ""
            if overall_text:
                conclusion_parts += f"<p style='font-size:1.05rem;font-weight:600;'>{escape(overall_text)}</p>"
            if conclusion_summary:
                conclusion_parts += f"<p style='margin-top:8px;'>{escape(conclusion_summary)}</p>"
            if version_info or test_summary:
                conclusion_parts += "<ul style='margin-top:8px;padding-left:20px;'>"
                if version_info:
                    conclusion_parts += f"<li><strong>版本</strong>: {escape(version_info)}</li>"
                if test_summary:
                    conclusion_parts += f"<li><strong>测试</strong>: {escape(test_summary)}</li>"
                conclusion_parts += "</ul>"
            conclusion_section = f"""
        <section>
            <h2>📊 结论</h2>
            {conclusion_parts}
        </section>"""

        # 部署信息
        deploy = data["deploy_result"]
        deploy_section = ""
        if deploy:
            deploy_items = "".join(
                f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>"
                for k, v in deploy.items()
            )
            deploy_section = f"""
        <section>
            <h2>🚀 部署信息</h2>
            <dl class="info-list">{deploy_items}</dl>
        </section>"""

        # 补充说明
        summary = data["summary"]
        summary_section = ""
        if summary:
            summary_section = f"""
        <section>
            <h2>📝 补充说明</h2>
            <p>{escape(summary)}</p>
        </section>"""

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ILA 迭代报告 #{escape(task_id)}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f7fa; color: #333; line-height: 1.6; padding: 24px;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        header {{
            background: #fff; border-radius: 12px; padding: 28px 32px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px;
        }}
        header h1 {{ font-size: 1.5rem; margin-bottom: 16px; color: #1a1a2e; }}
        .info-grid {{
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
        }}
        .info-item {{
            background: #f8f9fb; border-radius: 8px; padding: 12px 16px;
        }}
        .info-item .label {{ font-size: 0.8rem; color: #888; margin-bottom: 4px; }}
        .info-item .value {{ font-weight: 600; color: #1a1a2e; }}
        .verdict-bar {{
            display: flex; align-items: center; gap: 8px;
            padding: 14px 20px; border-radius: 8px; margin-top: 16px;
            font-size: 1.1rem; font-weight: 700;
        }}
        .verdict-pass {{ background: #e8f5e9; color: #2e7d32; }}
        .verdict-fail {{ background: #ffebee; color: #c62828; }}
        .verdict-degraded {{ background: #fff8e1; color: #e65100; }}
        .verdict-pending {{ background: #f5f5f5; color: #757575; }}
        section {{
            background: #fff; border-radius: 12px; padding: 24px 28px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px;
        }}
        section h2 {{ font-size: 1.15rem; margin-bottom: 16px; color: #1a1a2e; }}
        .data-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
        .data-table th {{
            text-align: left; padding: 10px 14px; border-bottom: 2px solid #e8eaf0;
            color: #667; font-weight: 600; font-size: 0.85rem; text-transform: uppercase;
        }}
        .data-table td {{ padding: 10px 14px; border-bottom: 1px solid #f0f2f5; }}
        .data-table td.pass {{ color: #2e7d32; font-weight: 600; }}
        .data-table td.fail {{ color: #c62828; font-weight: 600; }}
        .badge {{
            display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 0.75rem; font-weight: 600; color: #fff;
        }}
        .badge-bugfix {{ background: #ef5350; }}
        .badge-feature {{ background: #42a5f5; }}
        .badge-refactor {{ background: #ab47bc; }}
        .badge-optimization {{ background: #66bb6a; }}
        .stats {{ margin-top: 12px; display: flex; gap: 20px; }}
        .stat-item {{ font-size: 0.9rem; color: #667; }}
        .stat-regression {{ color: #c62828; font-weight: 600; }}
        {rollback_css}
        .info-list dt {{ font-weight: 600; color: #555; margin-top: 8px; }}
        .info-list dd {{ margin-left: 0; color: #333; margin-bottom: 4px; }}
        .muted {{ color: #aaa; }}
        footer {{ text-align: center; color: #aaa; font-size: 0.8rem; padding: 16px; }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>ILA 迭代报告 <span class="muted">#{escape(task_id)}</span></h1>
        <div class="info-grid">
            <div class="info-item">
                <div class="label">📦 目标对象</div>
                <div class="value">{escape(obj['object_id'])}</div>
            </div>
            <div class="info-item">
                <div class="label">🔄 版本</div>
                <div class="value">v{escape(current_ver)} → v{escape(new_ver)}</div>
            </div>
            <div class="info-item">
                <div class="label">📋 变更</div>
                <div class="value">{change_count} 项{f" ({change_summary})" if change_summary else ""}</div>
            </div>
            <div class="info-item">
                <div class="label">🔧 平台</div>
                <div class="value">{escape(data['platform'])}</div>
            </div>
        </div>
        <div class="verdict-bar {verdict_class}">
            {icon} 判定: {escape(label)}
        </div>
    </header>
    {rollback_section}{changes_section}{test_section}{process_section}{conclusion_section}{deploy_section}{summary_section}
    <footer>生成时间: {escape(generated_at)} | ILA Agent</footer>
</div>
</body>
</html>"""
