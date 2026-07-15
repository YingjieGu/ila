"""Tests for Reporter."""

from __future__ import annotations

import json
import os

import pytest
from ila.core.reporter import Reporter
from ila.models.managed_object import ManagedObject
from ila.models.task_spec import ChangeItem, TaskSpec, TestRequirements
from ila.models.test_result import TestCaseResult, TestResult


# ---- Fixtures ----


@pytest.fixture
def reporter():
    """创建 Reporter 实例."""
    return Reporter()


@pytest.fixture
def sample_object():
    """创建测试目标对象."""
    return ManagedObject(
        object_id="hermes:skill:my-skill",
        platform="hermes",
        object_type="skill",
        name="my-skill",
        path="/home/user/.hermes/skills/my-skill",
        current_version="1.0.0",
        metadata={"author": "test"},
    )


@pytest.fixture
def pass_task_spec():
    """创建 pass 场景的任务规格书."""
    return TaskSpec(
        task_id="ila-20260714-001",
        target_object_id="hermes:skill:my-skill",
        target_platform="hermes",
        target_path="/home/user/.hermes/skills/my-skill",
        current_version="1.0.0",
        requirement="修复中文编码 bug 并添加日志功能",
        changes=[
            ChangeItem(
                change_type="bugfix",
                description="修复 UTF-8 编码崩溃问题",
                files=["handler.py"],
                estimated_complexity="low",
            ),
            ChangeItem(
                change_type="feature",
                description="添加结构化日志输出",
                files=["logger.py", "handler.py"],
                estimated_complexity="medium",
            ),
        ],
        test_requirements=TestRequirements(
            functional=["日志输出正确"],
            regression=["已有功能不受影响"],
        ),
        sandbox_level="tempdir",
        rollback_plan="回滚到 v1.0.0",
    )


@pytest.fixture
def pass_test_results():
    """创建 pass 场景的测试结果."""
    result = TestResult(
        task_id="ila-20260714-001",
        object_id="hermes:skill:my-skill",
        verdict="pass",
        case_results=[
            TestCaseResult(
                case_id="test-1",
                test_type="functional",
                a1_output="old output",
                a2_output="new output",
                a1_pass=True,
                a2_pass=True,
                a1_latency_ms=50.0,
                a2_latency_ms=45.0,
            ),
            TestCaseResult(
                case_id="test-2",
                test_type="regression",
                a1_output="ok",
                a2_output="ok",
                a1_pass=True,
                a2_pass=True,
            ),
        ],
        summary="全部测试通过，新版功能正常。",
    )
    return result.to_dict()


@pytest.fixture
def pass_deploy_result():
    """创建 pass 场景的部署结果."""
    return {
        "status": "verified",
        "deployed_at": "2026-07-14T10:30:00",
        "verification_passed": True,
    }


@pytest.fixture
def pass_swap_result():
    """创建 pass 场景的热切换结果."""
    return {
        "new_version": "1.1.0",
        "rollback_snapshot": "/tmp/ila/snapshots/my-skill-v1.0.0.tar.gz",
        "swapped_at": "2026-07-14T10:35:00",
    }


@pytest.fixture
def fail_task_spec():
    """创建 fail 场景的任务规格书."""
    return TaskSpec(
        task_id="ila-20260714-002",
        target_object_id="hermes:skill:my-skill",
        target_platform="hermes",
        target_path="/home/user/.hermes/skills/my-skill",
        current_version="1.0.0",
        requirement="优化性能",
        changes=[
            ChangeItem(
                change_type="optimization",
                description="重写核心算法",
                files=["core.py"],
                estimated_complexity="high",
            ),
        ],
    )


@pytest.fixture
def fail_test_results():
    """创建 fail 场景的测试结果 (有回归)."""
    result = TestResult(
        task_id="ila-20260714-002",
        object_id="hermes:skill:my-skill",
        verdict="fail",
        case_results=[
            TestCaseResult(
                case_id="test-1",
                test_type="functional",
                a1_pass=True,
                a2_pass=True,
            ),
            TestCaseResult(
                case_id="test-2",
                test_type="regression",
                a1_pass=True,
                a2_pass=False,
            ),
        ],
        summary="存在回归: test-2 失败。",
    )
    return result.to_dict()


@pytest.fixture
def fail_swap_result():
    """创建 fail 场景的热切换结果 (回滚)."""
    return {
        "new_version": "1.1.0",
        "rollback_snapshot": "/tmp/ila/snapshots/my-skill-v1.0.0.tar.gz",
        "swapped_at": "",
    }


# ---- 测试类 ----


class TestReporterGenerate:
    """测试报告生成."""

    def test_generate_returns_three_formats(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """generate() 应返回 json / markdown / html 三种格式."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        assert set(report.keys()) == {"json", "markdown", "html"}
        for fmt in ("json", "markdown", "html"):
            assert isinstance(report[fmt], str)
            assert len(report[fmt]) > 0

    # ---- JSON 格式 ----

    def test_json_valid_structure(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """JSON 报告应是合法 JSON 且包含关键结构."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        data = json.loads(report["json"])

        assert data["task_id"] == "ila-20260714-001"
        assert data["object"]["object_id"] == "hermes:skill:my-skill"
        assert data["verdict"] == "pass"
        assert data["new_version"] == "1.1.0"
        assert data["current_version"] == "1.0.0"
        assert data["platform"] == "hermes"
        assert data["rollback_point"] == pass_swap_result["rollback_snapshot"]
        assert data["change_count"] == 2
        assert data["change_type_counts"] == {"bugfix": 1, "feature": 1}
        assert data["total_cases"] == 2
        assert data["passed_cases"] == 2
        assert data["failed_cases"] == 0
        assert data["regression_count"] == 0
        assert "generated_at" in data
        assert "task_spec" in data
        assert "test_results" in data
        assert "deploy_result" in data
        assert "swap_result" in data

    def test_json_contains_case_results(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """JSON 报告应包含测试用例明细."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        data = json.loads(report["json"])
        cases = data["case_results"]
        assert len(cases) == 2
        assert cases[0]["case_id"] == "test-1"
        assert cases[1]["case_id"] == "test-2"

    # ---- Markdown 格式 ----

    def test_markdown_contains_task_id(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含 task_id."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "ila-20260714-001" in md

    def test_markdown_contains_verdict(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含判定结果."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "✅" in md
        assert "通过" in md

    def test_markdown_contains_object_and_version(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含目标对象和版本变更."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "hermes:skill:my-skill" in md
        assert "1.0.0" in md
        assert "1.1.0" in md

    def test_markdown_contains_change_summary(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含变更摘要."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "1 bugfix + 1 feature" in md

    def test_markdown_contains_ab_test_table(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含 A/B 测试对比表."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "| 测试项 | 类型 | A1 (旧版) | A2 (新版) | 状态 |" in md
        assert "test-1" in md
        assert "test-2" in md
        assert "PASS" in md

    def test_markdown_contains_rollback_point(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含回滚点路径."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert pass_swap_result["rollback_snapshot"] in md

    def test_markdown_contains_platform(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含平台信息."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "hermes" in md

    def test_markdown_contains_change_list(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """Markdown 报告应包含变更清单明细."""
        md = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["markdown"]
        assert "变更清单" in md
        assert "修复 UTF-8 编码崩溃问题" in md
        assert "添加结构化日志输出" in md

    # ---- HTML 格式 ----

    def test_html_is_valid_structure(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应是合法的 HTML 结构."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<head>" in html
        assert "<body>" in html

    def test_html_contains_inline_css(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应包含内联 CSS 样式."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert "<style>" in html
        assert "</style>" in html

    def test_html_contains_task_id(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应包含 task_id."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert "ila-20260714-001" in html

    def test_html_contains_verdict(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应包含判定结果."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert "通过" in html
        assert "verdict-pass" in html

    def test_html_contains_ab_test_table(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应包含 A/B 测试对比表."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert "<table" in html
        assert "test-1" in html
        assert "test-2" in html
        assert "PASS" in html

    def test_html_contains_rollback_point(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应包含回滚点路径."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert pass_swap_result["rollback_snapshot"] in html

    def test_html_no_external_dependencies(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告不应有外部依赖 (无 link/style 外链)."""
        html = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )["html"]
        assert 'href="http' not in html
        assert 'src="http' not in html

    def test_html_escapes_special_chars(
        self, reporter, sample_object, pass_task_spec,
        pass_deploy_result, pass_swap_result,
    ):
        """HTML 报告应转义特殊字符."""
        # 构造含特殊字符的测试结果
        test_results = TestResult(
            task_id="ila-20260714-001",
            object_id="hermes:skill:my-skill",
            verdict="pass",
            case_results=[
                TestCaseResult(
                    case_id="<script>alert(1)</script>",
                    test_type="functional",
                    a1_pass=True,
                    a2_pass=True,
                ),
            ],
            summary="<b>bold</b> summary",
        )
        html = reporter.generate(
            sample_object, pass_task_spec,
            test_results.to_dict(), pass_deploy_result, pass_swap_result,
        )["html"]
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestReporterFailScenario:
    """测试 fail 场景."""

    def test_fail_json_verdict(
        self, reporter, sample_object, fail_task_spec,
        fail_test_results, pass_deploy_result, fail_swap_result,
    ):
        """fail 场景 JSON 报告 verdict 应为 fail."""
        report = reporter.generate(
            sample_object, fail_task_spec,
            fail_test_results, pass_deploy_result, fail_swap_result,
        )
        data = json.loads(report["json"])
        assert data["verdict"] == "fail"
        assert data["failed_cases"] == 1
        assert data["regression_count"] == 1

    def test_fail_markdown_shows_failure(
        self, reporter, sample_object, fail_task_spec,
        fail_test_results, pass_deploy_result, fail_swap_result,
    ):
        """fail 场景 Markdown 报告应显示失败."""
        md = reporter.generate(
            sample_object, fail_task_spec,
            fail_test_results, pass_deploy_result, fail_swap_result,
        )["markdown"]
        assert "❌" in md
        assert "失败" in md
        assert "FAIL" in md

    def test_fail_markdown_shows_regression(
        self, reporter, sample_object, fail_task_spec,
        fail_test_results, pass_deploy_result, fail_swap_result,
    ):
        """fail 场景 Markdown 应显示回归统计."""
        md = reporter.generate(
            sample_object, fail_task_spec,
            fail_test_results, pass_deploy_result, fail_swap_result,
        )["markdown"]
        assert "回归" in md

    def test_fail_html_verdict_class(
        self, reporter, sample_object, fail_task_spec,
        fail_test_results, pass_deploy_result, fail_swap_result,
    ):
        """fail 场景 HTML 应使用 verdict-fail 样式."""
        html = reporter.generate(
            sample_object, fail_task_spec,
            fail_test_results, pass_deploy_result, fail_swap_result,
        )["html"]
        assert "verdict-fail" in html
        assert "失败" in html

    def test_fail_html_shows_fail_in_table(
        self, reporter, sample_object, fail_task_spec,
        fail_test_results, pass_deploy_result, fail_swap_result,
    ):
        """fail 场景 HTML 表格应显示 FAIL."""
        html = reporter.generate(
            sample_object, fail_task_spec,
            fail_test_results, pass_deploy_result, fail_swap_result,
        )["html"]
        assert "FAIL" in html


class TestReporterSaveReport:
    """测试报告保存."""

    def test_save_creates_three_files(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result, tmp_path,
    ):
        """save_report 应创建三个文件."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        paths = reporter.save_report(report, str(tmp_path / "reports"), "ila-20260714-001")

        assert set(paths.keys()) == {"json", "markdown", "html"}
        for path in paths.values():
            assert os.path.exists(path)

    def test_save_correct_extensions(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result, tmp_path,
    ):
        """文件扩展名应正确."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        paths = reporter.save_report(report, str(tmp_path), "ila-001")

        assert paths["json"].endswith("ila-001.json")
        assert paths["markdown"].endswith("ila-001.md")
        assert paths["html"].endswith("ila-001.html")

    def test_save_creates_output_dir(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result, tmp_path,
    ):
        """输出目录不存在时应自动创建."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        output_dir = str(tmp_path / "deep" / "nested" / "reports")
        paths = reporter.save_report(report, output_dir, "ila-001")

        assert os.path.isdir(output_dir)
        for path in paths.values():
            assert os.path.exists(path)

    def test_save_file_contents_match(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result, tmp_path,
    ):
        """保存的文件内容应与生成的报告一致."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        paths = reporter.save_report(report, str(tmp_path), "ila-001")

        with open(paths["json"], encoding="utf-8") as f:
            assert f.read() == report["json"]
        with open(paths["markdown"], encoding="utf-8") as f:
            assert f.read() == report["markdown"]
        with open(paths["html"], encoding="utf-8") as f:
            assert f.read() == report["html"]

    def test_save_utf8_encoding(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result, pass_swap_result, tmp_path,
    ):
        """保存的文件应正确处理 UTF-8 中文内容."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, pass_swap_result,
        )
        paths = reporter.save_report(report, str(tmp_path), "ila-001")

        with open(paths["markdown"], encoding="utf-8") as f:
            content = f.read()
        assert "修复" in content
        assert "通过" in content


class TestReporterEdgeCases:
    """测试边界情况."""

    def test_empty_test_results(
        self, reporter, sample_object, pass_task_spec,
        pass_deploy_result, pass_swap_result,
    ):
        """空测试结果 (无用例) 时报告应正常生成."""
        test_results = {
            "task_id": "ila-001",
            "object_id": "hermes:skill:my-skill",
            "verdict": "pending",
            "total_cases": 0,
            "passed_cases": 0,
            "failed_cases": 0,
            "regression_count": 0,
            "case_results": [],
            "compatibility": {},
            "summary": "",
        }
        report = reporter.generate(
            sample_object, pass_task_spec,
            test_results, pass_deploy_result, pass_swap_result,
        )
        assert "pending" in report["json"]
        assert "无测试用例" in report["markdown"]
        # HTML 应正常生成
        assert "</html>" in report["html"]

    def test_no_rollback_point(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_deploy_result,
    ):
        """无回滚点时报告应正常生成."""
        swap_result = {"new_version": "1.1.0", "rollback_snapshot": ""}
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, pass_deploy_result, swap_result,
        )
        md = report["markdown"]
        assert "回滚点" not in md
        html = report["html"]
        assert "rollback-bar" not in html

    def test_no_changes(
        self, reporter, sample_object, pass_deploy_result, pass_swap_result,
    ):
        """无变更项时报告应正常生成."""
        task_spec = TaskSpec(
            task_id="ila-003",
            target_object_id="hermes:skill:my-skill",
            target_platform="hermes",
            target_path="/tmp",
            current_version="1.0.0",
            requirement="test requirement",
        )
        test_results = {
            "verdict": "pass",
            "total_cases": 0,
            "passed_cases": 0,
            "failed_cases": 0,
            "regression_count": 0,
            "case_results": [],
            "summary": "",
        }
        report = reporter.generate(
            sample_object, task_spec,
            test_results, pass_deploy_result, pass_swap_result,
        )
        data = json.loads(report["json"])
        assert data["change_count"] == 0
        assert data["change_type_counts"] == {}

    def test_no_deploy_result(
        self, reporter, sample_object, pass_task_spec,
        pass_test_results, pass_swap_result,
    ):
        """无部署结果时报告应正常生成."""
        report = reporter.generate(
            sample_object, pass_task_spec,
            pass_test_results, {}, pass_swap_result,
        )
        md = report["markdown"]
        assert "部署信息" not in md
        html = report["html"]
        assert "</html>" in html

    def test_degraded_verdict(
        self, reporter, sample_object, pass_task_spec,
        pass_deploy_result, pass_swap_result,
    ):
        """degraded 判定应正确呈现."""
        test_results = {
            "verdict": "degraded",
            "total_cases": 2,
            "passed_cases": 1,
            "failed_cases": 1,
            "regression_count": 0,
            "case_results": [
                {"case_id": "t1", "test_type": "functional", "a1_pass": True, "a2_pass": True},
                {"case_id": "t2", "test_type": "performance", "a1_pass": True, "a2_pass": False},
            ],
            "summary": "性能下降但功能正常。",
        }
        report = reporter.generate(
            sample_object, pass_task_spec,
            test_results, pass_deploy_result, pass_swap_result,
        )
        assert "degraded" in report["json"]
        assert "降级" in report["markdown"]
        assert "verdict-degraded" in report["html"]
