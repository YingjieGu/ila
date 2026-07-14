"""TestResult: A/B 测试结果数据模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestCaseResult:
    """单个测试用例的执行结果."""

    case_id: str
    test_type: str  # 'functional' | 'regression' | 'performance' | 'security' | 'compatibility'
    a1_output: str = ""
    a2_output: str = ""
    a1_pass: bool = False
    a2_pass: bool = False
    a1_latency_ms: float = 0.0
    a2_latency_ms: float = 0.0
    expected_output: str = ""
    error: str = ""

    @property
    def latency_delta_ms(self) -> float:
        """延迟变化 (ms), 正数表示新版更慢."""
        return self.a2_latency_ms - self.a1_latency_ms

    @property
    def is_regression(self) -> bool:
        """是否回归 (旧版通过但新版失败)."""
        return self.a1_pass and not self.a2_pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "test_type": self.test_type,
            "a1_output": self.a1_output,
            "a2_output": self.a2_output,
            "a1_pass": self.a1_pass,
            "a2_pass": self.a2_pass,
            "a1_latency_ms": self.a1_latency_ms,
            "a2_latency_ms": self.a2_latency_ms,
            "latency_delta_ms": self.latency_delta_ms,
            "is_regression": self.is_regression,
            "expected_output": self.expected_output,
            "error": self.error,
        }


@dataclass
class TestResult:
    """A/B 对比测试的完整结果.

    Attributes:
        task_id: 关联的任务 ID
        object_id: 被测试的对象 ID
        verdict: 综合判定 (``pass`` | ``fail`` | ``degraded``)
        case_results: 各测试用例的结果
        compatibility: 兼容性测试结果
        summary: 人类可读的摘要
    """

    task_id: str = ""
    object_id: str = ""
    verdict: str = "pending"  # 'pass' | 'fail' | 'degraded' | 'pending'
    case_results: list[TestCaseResult] = field(default_factory=list)
    compatibility: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def total_cases(self) -> int:
        return len(self.case_results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for c in self.case_results if c.a2_pass)

    @property
    def failed_cases(self) -> int:
        return sum(1 for c in self.case_results if not c.a2_pass)

    @property
    def regression_count(self) -> int:
        return sum(1 for c in self.case_results if c.is_regression)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "object_id": self.object_id,
            "verdict": self.verdict,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "regression_count": self.regression_count,
            "case_results": [c.to_dict() for c in self.case_results],
            "compatibility": self.compatibility,
            "summary": self.summary,
        }
