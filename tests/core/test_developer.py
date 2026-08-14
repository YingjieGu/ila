"""Developer 模块测试 (Windows 兼容、握手、错误信息)."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from ila.core.developer import Developer


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestClaudeHandshake:
    """F2/F3: 握手验证 CLI + API 连通性, 失败降级重试一次."""

    def test_handshake_success_first_try(self):
        with patch("subprocess.run", return_value=_FakeResult(0, "Pong!")) as mock:
            dev = Developer.__new__(Developer)
            ok, err = dev._claude_handshake()
        assert ok is True
        assert err == ""
        # 成功时只调用一次 (无重试)
        assert mock.call_count == 1
        # 使用 --print ping 验证 API 连通性 (而非仅 --version)
        args = mock.call_args[0][0]
        assert "--print" in args and "ping" in args

    def test_handshake_retry_on_timeout(self):
        """偶发超时 (Windows cmd 包装开销) 时降级重试一次."""
        from subprocess import TimeoutExpired

        with patch("subprocess.run", side_effect=[
            TimeoutExpired("claude", 5),       # 第一次超时
            _FakeResult(0, "Pong!"),           # 重试成功
        ]) as mock:
            dev = Developer.__new__(Developer)
            ok, err = dev._claude_handshake()
        assert ok is True
        assert mock.call_count == 2

    def test_handshake_fails_after_two_attempts(self):
        from subprocess import TimeoutExpired

        with patch("subprocess.run", side_effect=[
            TimeoutExpired("claude", 5),
            TimeoutExpired("claude", 5),
        ]) as mock:
            dev = Developer.__new__(Developer)
            ok, err = dev._claude_handshake()
        assert ok is False
        assert "超时" in err
        assert mock.call_count == 2

    def test_handshake_cli_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            dev = Developer.__new__(Developer)
            ok, err = dev._claude_handshake()
        assert ok is False
        assert "未安装" in err

    def test_handshake_captures_api_error(self):
        """F3: 订阅过期等 API 错误在握手阶段即暴露."""
        err_out = "Error: API Error: 400 ... no valid CodingPlan subscription"
        with patch("subprocess.run", return_value=_FakeResult(1, err_out, "")):
            dev = Developer.__new__(Developer)
            ok, err = dev._claude_handshake()
        assert ok is False
        assert "CodingPlan" in err


class TestClaudeErrorChannel:
    """F1: claude 错误输出在 stdout, 拼接两个通道取末尾."""

    def _make_dev(self):
        dev = Developer.__new__(Developer)
        dev.timeout = 60
        dev.config = {}
        dev.framework = "claude_code"
        dev.codex_model = "deepseek-v4-pro"
        dev.codex_sandbox_mode = "bypass"
        dev.max_retries = 3
        return dev

    def _fake_task_spec(self):
        """构造最小 TaskSpec (仅需 changes 属性供 _build_prompt)."""
        class _Change:
            description = "test change"
            files = ["file.py"]
        class _Spec:
            changes = [_Change()]
            requirement = "test"
        return _Spec()

    def test_error_from_stdout_when_stderr_empty(self):
        """真实场景: returncode=1, 错误在 stdout, stderr 为空."""
        stdout_err = "Error: API Error: 400 ... no valid CodingPlan subscription"
        with patch("subprocess.run", return_value=_FakeResult(1, stdout_err, "")) as mock:
            dev = self._make_dev()
            result = dev._develop_with_claude_code("/tmp/sb", self._fake_task_spec())
        assert result["status"] == "error"
        # 错误原因包含 stdout 中的 API 错误 (而非空)
        assert "CodingPlan" in result["reason"]
        # 传入 --print 且使用完整路径/命令
        args = mock.call_args[0][0]
        assert "--print" in args

    def test_error_from_stderr(self):
        stderr_err = "fatal: something went wrong"
        with patch("subprocess.run", return_value=_FakeResult(2, "", stderr_err)):
            dev = self._make_dev()
            result = dev._develop_with_claude_code("/tmp/sb", self._fake_task_spec())
        assert result["status"] == "error"
        assert "went wrong" in result["reason"]

    def test_handshake_failure_short_circuits_develop(self):
        """握手失败时不进入实际开发调用."""
        with patch("subprocess.run", return_value=_FakeResult(1, "no valid CodingPlan subscription", "")):
            dev = self._make_dev()
            result = dev._develop_with_claude_code("/tmp/sb", self._fake_task_spec())
        assert result["status"] == "error"
        assert "不可用" in result["reason"]
        assert "CodingPlan" in result["reason"]


class TestCodexGuidance:
    """开发框架缺失时的安装引导."""

    def test_codex_missing_returns_guidance(self):
        class _Change:
            description = "test change"
            files = ["file.py"]
        class _Spec:
            changes = [_Change()]
            requirement = "test"
        with patch("shutil.which", return_value=None):
            dev = Developer.__new__(Developer)
            dev.config = {}
            dev.framework = "codex"
            dev.codex_model = "deepseek-v4-pro"
            dev.codex_sandbox_mode = "bypass"
            result = dev._develop_with_codex("/tmp/sb", _Spec())
        assert result["status"] == "error"
        assert "npm install -g @openai/codex" in result["reason"]
        assert "framework" in result["reason"]
