"""A/B 对比测试模块 — 通过适配器对比新旧版本."""

from __future__ import annotations

import logging
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject
from ila.models.test_result import TestCaseResult, TestResult

logger = logging.getLogger(__name__)


class ABTester:
    """A/B 对比测试器.

    通过平台适配器分别调用旧版本（线上）和新版本（staging），
    对比输出结果，自动判定通过/失败/退化。
    """

    def __init__(self, adapter: PlatformAdapter, timeout: int = 60,
                 performance_threshold: float = 1.2):
        """初始化测试器.

        Args:
            adapter: 平台适配器
            timeout: 单次测试超时秒数
            performance_threshold: 性能退化阈值 (新版延迟/旧版延迟)
        """
        self.adapter = adapter
        self.timeout = timeout
        self.performance_threshold = performance_threshold

    def test(self, obj: ManagedObject, sandbox_path: str,
             test_cases: list[dict[str, Any]]) -> TestResult:
        """执行 A/B 对比测试.

        Args:
            obj: 被测试的目标对象
            sandbox_path: 新版本沙箱路径
            test_cases: 测试用例列表，每个用例:
                ``{"id": "...", "type": "functional", "input": {"prompt": "..."}, "expected": "..."}``

        Returns:
            TestResult 完整测试结果
        """
        # 1. 部署新版本到 staging
        logger.info("部署新版本到 staging: %s", obj.name)
        staging_id = self.adapter.deploy_to_staging(obj, sandbox_path)

        # 2. 执行 A/B 测试
        result = TestResult(
            task_id="",
            object_id=obj.object_id,
            verdict="pending",
        )

        for case in test_cases:
            case_result = self._run_single_case(obj, staging_id, case)
            result.case_results.append(case_result)
            logger.info(
                "测试用例 %s: A1=%s A2=%s",
                case.get("id", "?"),
                "PASS" if case_result.a1_pass else "FAIL",
                "PASS" if case_result.a2_pass else "FAIL",
            )

        # 3. 兼容性测试
        logger.info("验证兼容性...")
        result.compatibility = self.adapter.validate_compatibility(obj, sandbox_path)

        # 4. 清理 staging
        self.adapter.cleanup_staging(staging_id)

        # 5. 综合判定
        result.verdict = self._determine_verdict(result)
        result.summary = self._generate_summary(result)

        return result

    def _run_single_case(self, obj: ManagedObject, staging_id: str,
                         case: dict[str, Any]) -> TestCaseResult:
        """执行单个测试用例的 A/B 对比."""
        case_id = case.get("id", "unknown")
        test_type = case.get("type", "functional")
        test_input = case.get("input", {})
        expected = case.get("expected", "")

        case_result = TestCaseResult(
            case_id=case_id,
            test_type=test_type,
            expected_output=expected,
        )

        # A1: 测试旧版本 (线上)
        try:
            start = time.monotonic()
            a1_result = self.adapter.invoke_object(obj, test_input)
            case_result.a1_latency_ms = (time.monotonic() - start) * 1000
            case_result.a1_output = a1_result.get("output", "")
            case_result.a1_pass = self._check_pass(a1_result, expected)
        except Exception as e:
            logger.warning("A1 测试异常 (%s): %s", case_id, e)
            case_result.a1_output = ""
            case_result.a1_pass = False
            case_result.error = f"A1: {e}"

        # A2: 测试新版本 (staging)
        try:
            staging_input = dict(test_input)
            staging_input["skill"] = obj.name
            start = time.monotonic()
            a2_result = self.adapter.invoke_staging(staging_id, staging_input)
            case_result.a2_latency_ms = (time.monotonic() - start) * 1000
            case_result.a2_output = a2_result.get("output", "")
            case_result.a2_pass = self._check_pass(a2_result, expected)
        except Exception as e:
            logger.warning("A2 测试异常 (%s): %s", case_id, e)
            case_result.a2_output = ""
            case_result.a2_pass = False
            if case_result.error:
                case_result.error += f"; A2: {e}"
            else:
                case_result.error = f"A2: {e}"

        return case_result

    def _check_pass(self, result: dict[str, Any], expected: str) -> bool:
        """检查测试结果是否通过."""
        # exit_code != 0 → 失败
        if result.get("exit_code", 1) != 0:
            return False
        # 有 error 信息 → 失败 (除非 error 为空)
        error = result.get("error", "")
        if error:
            return False
        if expected:
            output = result.get("output", "")
            # 支持子串匹配和精确匹配
            if expected in output or output.strip() == expected.strip():
                return True
            return False
        # 没有期望值，只要 exit_code == 0 且无 error 就算通过
        return True

    def _determine_verdict(self, result: TestResult) -> str:
        """综合判定: pass / fail / degraded."""
        # 有回归 → fail
        if result.regression_count > 0:
            return "fail"

        # 兼容性不通过 → fail
        if not result.compatibility.get("compatible", True):
            return "fail"

        # 有失败用例 → degraded (新版有问题但不是回归)
        if result.failed_cases > 0:
            return "degraded"

        # 全部通过 → pass
        if result.total_cases > 0 and result.passed_cases == result.total_cases:
            # 检查性能退化
            for case in result.case_results:
                if case.test_type == "performance":
                    if case.a1_latency_ms > 0:
                        ratio = case.a2_latency_ms / case.a1_latency_ms
                        if ratio > self.performance_threshold:
                            return "degraded"
            return "pass"

        # 没有测试用例 → pending
        return "pending"

    def _generate_summary(self, result: TestResult) -> str:
        """生成人类可读的测试摘要."""
        lines = [
            f"A/B 测试完成: {result.total_cases} 个用例",
            f"  通过: {result.passed_cases}, 失败: {result.failed_cases}",
            f"  回归: {result.regression_count}",
            f"  判定: {result.verdict}",
        ]
        if result.compatibility:
            if result.compatibility.get("compatible", True):
                lines.append("  兼容性: ✅ 通过")
            else:
                issues = result.compatibility.get("issues", [])
                lines.append(f"  兼容性: ❌ 不通过 ({', '.join(issues)})")
        return "\n".join(lines)

    def generate_default_test_cases(self, obj: ManagedObject,
                                     test_requirements: dict) -> list[dict[str, Any]]:
        """根据测试需求生成默认测试用例.

        自动检测技能文件结构，为带 HTML 页面的技能添加页面完整性测试。
        避免调用 hermes chat (太慢)。

        Args:
            obj: 目标对象
            test_requirements: TaskSpec 中的 test_requirements 字典

        Returns:
            测试用例列表
        """
        cases: list[dict[str, Any]] = []
        import os

        # 功能测试 - 检查 SKILL.md 存在且格式正确
        cases.append({
            "id": "func-skill-md",
            "type": "functional",
            "input": {"check_file": "SKILL.md"},
            "expected": "",
        })

        # 检测实际文件结构，针对性生成测试
        skill_path = obj.path
        if os.path.isdir(skill_path):
            # 回归测试 - 检查主要代码文件存在 (自动检测而非硬编码 handler.py)
            for code_file in ("handler.py", "main.py", "__init__.py"):
                if os.path.isfile(os.path.join(skill_path, code_file)):
                    cases.append({
                        "id": f"reg-{code_file.replace('.', '-')}-exists",
                        "type": "regression",
                        "input": {"check_file": code_file},
                        "expected": "",
                    })
                    break

            # 如果技能包含 HTML 页面，添加页面完整性测试
            html_files = [
                f for f in os.listdir(skill_path)
                if f.endswith(".html")
            ]
            for html_file in html_files:
                cases.append({
                    "id": f"func-html-{html_file.replace('.', '-')}",
                    "type": "functional",
                    "input": {
                        "check_file": html_file,
                        "expect_contains": "<html",
                    },
                    "expected": "<html",
                })

            # 如果技能包含 Python 主文件，添加语法检查测试
            py_main = None
            for py_file in ("minesweeper.py", "handler.py", "main.py"):
                if os.path.isfile(os.path.join(skill_path, py_file)):
                    py_main = py_file
                    break
            if py_main:
                cases.append({
                    "id": f"reg-py-syntax-{py_main.replace('.', '-')}",
                    "type": "regression",
                    "input": {"check_file": py_main},
                    "expected": "",
                })

        # 如果需求中提到特定内容，添加内容检查
        for desc in test_requirements.get("functional", []):
            if "Hello" in desc or "hello" in desc or "OK" in desc:
                cases.append({
                    "id": f"func-content-{len(cases)}",
                    "type": "functional",
                    "input": {"check_file": "handler.py", "expect_contains": "Hello"},
                    "expected": "Hello",
                })

        return cases
