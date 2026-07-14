"""Tests for ILA data models."""

import pytest
from ila.models.managed_object import ManagedObject
from ila.models.task_spec import TaskSpec, ChangeItem, TestRequirements
from ila.models.test_result import TestResult, TestCaseResult


class TestManagedObject:
    """ManagedObject 数据模型测试."""

    def test_basic_creation(self):
        obj = ManagedObject(
            object_id="hermes:skill:my-skill",
            platform="hermes",
            object_type="skill",
            name="my-skill",
            path="/home/user/.hermes/skills/my-skill",
            current_version="1.0.0",
        )
        assert obj.object_id == "hermes:skill:my-skill"
        assert obj.platform == "hermes"
        assert obj.object_type == "skill"
        assert obj.current_version == "1.0.0"

    def test_make_id(self):
        oid = ManagedObject.make_id("openclaw", "plugin", "weather")
        assert oid == "openclaw:plugin:weather"

    def test_invalid_object_id(self):
        with pytest.raises(ValueError, match="object_id 必须格式为"):
            ManagedObject(
                object_id="invalid-id",
                platform="hermes",
                object_type="skill",
                name="test",
                path="/tmp",
            )

    def test_default_version(self):
        obj = ManagedObject(
            object_id="hermes:skill:test",
            platform="hermes",
            object_type="skill",
            name="test",
            path="/tmp",
        )
        assert obj.current_version == "unknown"
        assert obj.metadata == {}

    def test_serialization(self):
        obj = ManagedObject(
            object_id="hermes:skill:my-skill",
            platform="hermes",
            object_type="skill",
            name="my-skill",
            path="/tmp/skill",
            current_version="1.2.0",
            metadata={"author": "test"},
        )
        d = obj.to_dict()
        assert d["object_id"] == "hermes:skill:my-skill"
        assert d["metadata"]["author"] == "test"

        obj2 = ManagedObject.from_dict(d)
        assert obj2.object_id == obj.object_id
        assert obj2.current_version == "1.2.0"
        assert obj2.metadata["author"] == "test"


class TestTaskSpec:
    """TaskSpec 数据模型测试."""

    def test_basic_creation(self):
        spec = TaskSpec(
            task_id="ila-20260714-001",
            target_object_id="hermes:skill:my-skill",
            target_platform="hermes",
            target_path="/tmp/skill",
            current_version="1.0.0",
            requirement="修复中文编码 bug",
        )
        assert spec.task_id == "ila-20260714-001"
        assert spec.requirement == "修复中文编码 bug"
        assert spec.sandbox_level == "tempdir"
        assert spec.changes == []

    def test_generate_task_id(self):
        tid = TaskSpec.generate_task_id()
        assert tid.startswith("ila-")
        assert len(tid) == 19  # ila-YYYYMMDD-HHMMSS

    def test_with_changes(self):
        spec = TaskSpec(
            task_id="ila-001",
            target_object_id="hermes:skill:test",
            target_platform="hermes",
            target_path="/tmp",
            current_version="1.0.0",
            requirement="add feature",
            changes=[
                ChangeItem(
                    change_type="feature",
                    description="add logging",
                    files=["handler.py"],
                    estimated_complexity="low",
                ),
            ],
            test_requirements=TestRequirements(
                functional=["log output correct"],
                regression=["existing features work"],
            ),
        )
        assert len(spec.changes) == 1
        assert spec.changes[0].change_type == "feature"
        assert spec.test_requirements.functional == ["log output correct"]

    def test_serialization(self):
        spec = TaskSpec(
            task_id="ila-001",
            target_object_id="hermes:skill:test",
            target_platform="hermes",
            target_path="/tmp",
            current_version="1.0.0",
            requirement="fix bug",
            changes=[ChangeItem(change_type="bugfix", description="fix crash")],
        )
        d = spec.to_dict()
        assert d["task_id"] == "ila-001"
        assert len(d["changes"]) == 1

        spec2 = TaskSpec.from_dict(d)
        assert spec2.task_id == "ila-001"
        assert len(spec2.changes) == 1
        assert spec2.changes[0].change_type == "bugfix"


class TestTestResult:
    """TestResult 数据模型测试."""

    def test_basic_creation(self):
        result = TestResult(
            task_id="ila-001",
            object_id="hermes:skill:test",
            verdict="pass",
        )
        assert result.verdict == "pass"
        assert result.total_cases == 0

    def test_case_results(self):
        result = TestResult(
            task_id="ila-001",
            object_id="hermes:skill:test",
            verdict="pass",
            case_results=[
                TestCaseResult(case_id="t1", test_type="functional", a1_pass=True, a2_pass=True),
                TestCaseResult(case_id="t2", test_type="regression", a1_pass=True, a2_pass=False),
            ],
        )
        assert result.total_cases == 2
        assert result.passed_cases == 1
        assert result.failed_cases == 1
        assert result.regression_count == 1

    def test_latency_delta(self):
        case = TestCaseResult(
            case_id="perf1",
            test_type="performance",
            a1_pass=True,
            a2_pass=True,
            a1_latency_ms=100.0,
            a2_latency_ms=120.0,
        )
        assert case.latency_delta_ms == 20.0

    def test_is_regression(self):
        case = TestCaseResult(
            case_id="r1",
            test_type="regression",
            a1_pass=True,
            a2_pass=False,
        )
        assert case.is_regression is True

        case2 = TestCaseResult(
            case_id="r2",
            test_type="regression",
            a1_pass=False,
            a2_pass=False,
        )
        assert case2.is_regression is False

    def test_serialization(self):
        result = TestResult(
            task_id="ila-001",
            object_id="hermes:skill:test",
            verdict="pass",
            case_results=[
                TestCaseResult(case_id="t1", test_type="functional", a1_pass=True, a2_pass=True),
            ],
        )
        d = result.to_dict()
        assert d["verdict"] == "pass"
        assert d["total_cases"] == 1
        assert d["passed_cases"] == 1
        assert len(d["case_results"]) == 1
