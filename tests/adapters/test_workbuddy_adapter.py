"""Tests for WorkBuddyAdapter."""

import os
import tarfile
from unittest.mock import patch

import pytest
from ila.adapters.workbuddy_adapter import WorkBuddyAdapter
from ila.models.managed_object import ManagedObject


@pytest.fixture
def workbuddy_home(tmp_path):
    """创建临时 WorkBuddy home 目录结构 (技能 + 专家)."""
    home = tmp_path / "workbuddy"

    # 技能: skills/<name>/SKILL.md + agent.py + pyproject.toml
    skill_dir = home / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: test\nversion: 1.0.0\n---\n# Test Skill\n",
        encoding="utf-8",
    )
    (skill_dir / "agent.py").write_text(
        "import sys\nprint('skill-ok')\n", encoding="utf-8",
    )
    (skill_dir / "pyproject.toml").write_text(
        "[project]\nname = \"test-skill\"\n", encoding="utf-8",
    )
    # 快照应跳过的内容
    pycache = skill_dir / "__pycache__" / "agent.cpython-312.pyc"
    pycache.parent.mkdir(parents=True)
    pycache.write_bytes(b"\x00\x01")

    # 无 SKILL.md 的目录 (应被跳过)
    no_def_dir = home / "skills" / "no-def"
    no_def_dir.mkdir(parents=True)
    (no_def_dir / "notes.txt").write_text("hello\n", encoding="utf-8")

    # 专家: plugins/marketplaces/my-experts/plugins/<name>/agents/*.yaml
    expert_dir = (
        home / "plugins" / "marketplaces" / "my-experts" / "plugins" / "test-expert"
    )
    agents_dir = expert_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "openai.yaml").write_text(
        "display_name: Test Expert\nshort_description: A test expert\n"
        "default_prompt: You are a test expert.\n",
        encoding="utf-8",
    )

    # 无 agents/*.yaml 的专家目录 (应被跳过)
    empty_expert_dir = (
        home / "plugins" / "marketplaces" / "my-experts" / "plugins" / "empty-expert"
    )
    empty_expert_dir.mkdir(parents=True)

    return str(home)


@pytest.fixture
def adapter(workbuddy_home):
    """创建适配器实例."""
    return WorkBuddyAdapter(workbuddy_home=workbuddy_home)


@pytest.fixture
def sample_skill(workbuddy_home):
    """创建测试技能对象."""
    return ManagedObject(
        object_id="workbuddy:skill:test-skill",
        platform="workbuddy",
        object_type="skill",
        name="test-skill",
        path=os.path.join(workbuddy_home, "skills", "test-skill"),
        current_version="1.0.0",
    )


@pytest.fixture
def sample_expert(workbuddy_home):
    """创建测试专家对象."""
    return ManagedObject(
        object_id="workbuddy:expert:test-expert",
        platform="workbuddy",
        object_type="expert",
        name="test-expert",
        path=os.path.join(
            workbuddy_home,
            "plugins", "marketplaces", "my-experts", "plugins", "test-expert",
        ),
    )


class TestIdentity:
    """测试平台标识."""

    def test_platform_id(self, adapter):
        assert adapter.platform_id() == "workbuddy"

    def test_get_platform_home(self, adapter, workbuddy_home):
        assert adapter.get_platform_home() == workbuddy_home


class TestDiscovery:
    """测试对象发现 (技能 + 专家)."""

    def test_discover_skill(self, adapter):
        objects = adapter.discover_objects()
        skills = [o for o in objects if o.object_type == "skill"]
        assert len(skills) == 1
        assert skills[0].object_id == "workbuddy:skill:test-skill"
        assert skills[0].current_version == "1.0.0"

    def test_discover_expert(self, adapter):
        objects = adapter.discover_objects()
        experts = [o for o in objects if o.object_type == "expert"]
        assert len(experts) == 1
        assert experts[0].object_id == "workbuddy:expert:test-expert"
        assert experts[0].path.endswith(os.path.join("test-expert"))

    def test_skip_dirs_without_definition(self, adapter):
        """无 SKILL.md / agents yaml 的目录不应被纳管."""
        objects = adapter.discover_objects()
        names = [o.name for o in objects]
        assert "no-def" not in names
        assert "empty-expert" not in names

    def test_discover_missing_home(self, tmp_path):
        """workbuddy_home 不存在时返回空列表 (容错)."""
        adapter = WorkBuddyAdapter(workbuddy_home=str(tmp_path / "nonexistent"))
        assert adapter.discover_objects() == []

    def test_discover_idempotent(self, adapter):
        """发现操作可重复且结果一致."""
        assert adapter.discover_objects() == adapter.discover_objects()

    def test_get_object_skill(self, adapter):
        obj = adapter.get_object("workbuddy:skill:test-skill")
        assert obj is not None
        assert obj.name == "test-skill"
        assert obj.current_version == "1.0.0"

    def test_get_object_expert(self, adapter):
        obj = adapter.get_object("workbuddy:expert:test-expert")
        assert obj is not None
        assert obj.name == "test-expert"

    def test_get_object_nonexistent(self, adapter):
        assert adapter.get_object("workbuddy:skill:nonexistent") is None
        assert adapter.get_object("workbuddy:expert:nonexistent") is None

    def test_get_object_bad_id(self, adapter):
        assert adapter.get_object("workbuddy:skill") is None

    def test_get_object_other_platform(self, adapter):
        assert adapter.get_object("hermes:skill:test-skill") is None


class TestSnapshot:
    """测试快照与恢复."""

    def test_create_snapshot(self, adapter, sample_skill):
        snapshot_path = adapter.create_snapshot(sample_skill)
        assert os.path.exists(snapshot_path)
        assert snapshot_path.endswith(".tar.gz")

        with tarfile.open(snapshot_path, "r:gz") as tar:
            names = tar.getnames()
            assert any(n.endswith("SKILL.md") for n in names)

    def test_snapshot_excludes_pycache(self, adapter, sample_skill):
        """快照应排除 __pycache__ 与 *.pyc."""
        snapshot_path = adapter.create_snapshot(sample_skill)
        with tarfile.open(snapshot_path, "r:gz") as tar:
            names = tar.getnames()
        assert not any("__pycache__" in n for n in names)
        assert not any(n.endswith(".pyc") for n in names)

    def test_snapshot_expert(self, adapter, sample_expert):
        snapshot_path = adapter.create_snapshot(sample_expert)
        with tarfile.open(snapshot_path, "r:gz") as tar:
            names = tar.getnames()
        assert any(n.endswith("openai.yaml") for n in names)

    def test_restore_snapshot(self, adapter, sample_skill):
        snapshot_path = adapter.create_snapshot(sample_skill)
        # 修改原文件
        skill_path = sample_skill.path
        with open(os.path.join(skill_path, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# modified\n")

        result = adapter.restore_snapshot(sample_skill, snapshot_path)
        assert result is True

        with open(os.path.join(skill_path, "SKILL.md"), encoding="utf-8") as f:
            content = f.read()
        assert "Test Skill" in content

    def test_restore_nonexistent_snapshot(self, adapter, sample_skill):
        result = adapter.restore_snapshot(
            sample_skill, os.path.join(adapter.workbuddy_home, "no.tar.gz")
        )
        assert result is False


class TestStaging:
    """测试 staging 部署."""

    def test_deploy_skill_staging(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text("# New\n", encoding="utf-8")

        staging_id = adapter.deploy_to_staging(sample_skill, str(sandbox))
        assert isinstance(staging_id, str)

        staging_path = os.path.join(
            adapter.workbuddy_home, ".ila-staging", "skills", "test-skill"
        )
        assert os.path.isfile(os.path.join(staging_path, "SKILL.md"))

    def test_deploy_expert_staging(self, adapter, sample_expert, tmp_path):
        sandbox = tmp_path / "sandbox"
        agents_dir = sandbox / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "openai.yaml").write_text("display_name: New\n", encoding="utf-8")

        staging_id = adapter.deploy_to_staging(sample_expert, str(sandbox))
        assert isinstance(staging_id, str)

        staging_path = os.path.join(
            adapter.workbuddy_home, ".ila-staging",
            "plugins", "marketplaces", "my-experts", "plugins", "test-expert",
        )
        assert os.path.isfile(os.path.join(staging_path, "agents", "openai.yaml"))

    def test_deploy_staging_html(self, adapter, sample_skill, tmp_path):
        """含 HTML 文件时返回带 staging_url 的 dict."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text("# New\n", encoding="utf-8")
        (sandbox / "index.html").write_text("<html></html>\n", encoding="utf-8")

        result = adapter.deploy_to_staging(sample_skill, str(sandbox))
        assert isinstance(result, dict)
        assert "staging_id" in result
        assert "staging_url" in result
        assert result["html_file"] == "index.html"


class TestInvoke:
    """测试对象调用."""

    def test_check_file_hit(self, adapter, sample_skill):
        result = adapter.invoke_object(
            sample_skill, {"check_file": "SKILL.md", "expect_contains": "Test Skill"}
        )
        assert result["exit_code"] == 0
        assert "Test Skill" in result["output"]

    def test_check_file_miss(self, adapter, sample_skill):
        result = adapter.invoke_object(sample_skill, {"check_file": "missing.py"})
        assert result["exit_code"] == 1
        assert "不存在" in result["error"]

    def test_expect_contains_miss(self, adapter, sample_skill):
        result = adapter.invoke_object(
            sample_skill, {"check_file": "SKILL.md", "expect_contains": "NOT_IN_FILE"}
        )
        assert result["exit_code"] == 0  # 文件存在
        assert "NOT_IN_FILE" in result["error"]

    def test_run_agent_execution(self, adapter, sample_skill):
        """含 agent.py 时通过 subprocess 执行验证可运行性."""
        result = adapter.invoke_object(sample_skill, {"run_agent": "--foo bar"})
        assert result["exit_code"] == 0
        assert "skill-ok" in result["output"]

    def test_run_agent_no_script(self, adapter, tmp_path):
        obj = ManagedObject(
            object_id="workbuddy:skill:fake",
            platform="workbuddy",
            object_type="skill",
            name="fake",
            path=str(tmp_path / "fake"),
        )
        os.makedirs(obj.path, exist_ok=True)
        result = adapter.invoke_object(obj, {"run_agent": True})
        assert result["exit_code"] == 1
        assert "agent.py" in result["error"]

    @patch("subprocess.run")
    def test_run_agent_failure(self, mock_run, adapter, sample_skill):
        """agent.py 执行失败时返回 exit_code=1 与 stderr."""
        mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        result = adapter.invoke_object(sample_skill, {"run_agent": True})
        assert result["exit_code"] == 1
        assert "boom" in result["error"]

    def test_invoke_staging_with_check_file(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text("# Staging\n", encoding="utf-8")
        staging_id = adapter.deploy_to_staging(sample_skill, str(sandbox))

        result = adapter.invoke_staging(
            staging_id, {"check_file": "SKILL.md", "expect_contains": "Staging"}
        )
        assert result["exit_code"] == 0
        assert "Staging" in result["output"]

    def test_invoke_staging_fallback_path(self, adapter, workbuddy_home):
        """兼容旧调用方式：仅凭 test_input 中的 skill 名称推导 staging 路径."""
        staging_path = os.path.join(
            workbuddy_home, ".ila-staging", "skills", "test-skill"
        )
        os.makedirs(staging_path, exist_ok=True)
        with open(os.path.join(staging_path, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# S\n")

        result = adapter.invoke_staging("legacy-id", {"skill": "test-skill"})
        assert result["exit_code"] == 0


class TestHotSwap:
    """测试热切换."""

    @patch.object(WorkBuddyAdapter, "health_check", return_value=True)
    def test_hot_swap_success(self, mock_health, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 2.0.0\n---\n# Updated\n", encoding="utf-8"
        )

        result = adapter.hot_swap(sample_skill, str(sandbox))
        assert result["status"] == "success"
        assert "snapshot" in result

        with open(os.path.join(sample_skill.path, "SKILL.md"), encoding="utf-8") as f:
            content = f.read()
        assert "2.0.0" in content

    @patch.object(WorkBuddyAdapter, "health_check", return_value=False)
    def test_hot_swap_rollback_on_health_fail(self, mock_health, adapter,
                                               sample_skill, tmp_path):
        original_path = sample_skill.path
        with open(os.path.join(original_path, "SKILL.md"), encoding="utf-8") as f:
            original_content = f.read()

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 9.0.0\n---\n# Bad\n", encoding="utf-8"
        )

        result = adapter.hot_swap(sample_skill, str(sandbox))
        assert result["status"] == "rolled_back"
        assert "health check" in result["reason"]

        with open(os.path.join(original_path, "SKILL.md"), encoding="utf-8") as f:
            restored_content = f.read()
        assert restored_content == original_content

    def test_hot_swap_expert(self, adapter, sample_expert, tmp_path):
        """专家热切换: 真实健康检查通过 (agents/*.yaml 存在)."""
        sandbox = tmp_path / "sandbox"
        agents_dir = sandbox / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "openai.yaml").write_text(
            "display_name: Updated\n", encoding="utf-8"
        )

        result = adapter.hot_swap(sample_expert, str(sandbox))
        assert result["status"] == "success"
        assert os.path.isfile(os.path.join(sample_expert.path, "agents", "openai.yaml"))


class TestHealth:
    """测试健康检查."""

    def test_skill_healthy(self, adapter, sample_skill):
        assert adapter.health_check(sample_skill) is True

    def test_skill_missing_skill_md(self, adapter, tmp_path):
        path = tmp_path / "bad-skill"
        path.mkdir()
        obj = ManagedObject(
            object_id="workbuddy:skill:bad",
            platform="workbuddy", object_type="skill", name="bad", path=str(path),
        )
        assert adapter.health_check(obj) is False

    def test_expert_healthy(self, adapter, sample_expert):
        assert adapter.health_check(sample_expert) is True

    def test_expert_missing_agents(self, adapter, tmp_path):
        path = tmp_path / "bad-expert"
        path.mkdir()
        obj = ManagedObject(
            object_id="workbuddy:expert:bad",
            platform="workbuddy", object_type="expert", name="bad", path=str(path),
        )
        assert adapter.health_check(obj) is False

    def test_missing_dir(self, adapter):
        obj = ManagedObject(
            object_id="workbuddy:skill:ghost",
            platform="workbuddy", object_type="skill", name="ghost",
            path=os.path.join(adapter.workbuddy_home, "skills", "ghost"),
        )
        assert adapter.health_check(obj) is False


class TestCompatibility:
    """测试兼容性验证."""

    def test_skill_compatible(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 1.1.0\n---\n# New\n", encoding="utf-8"
        )
        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is True
        assert len(result["issues"]) == 0

    def test_skill_missing_skill_md(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "handler.py").write_text("x = 1\n", encoding="utf-8")
        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is False
        assert any("SKILL.md" in i for i in result["issues"])

    def test_expert_compatible(self, adapter, sample_expert, tmp_path):
        sandbox = tmp_path / "sandbox"
        agents_dir = sandbox / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "openai.yaml").write_text("display_name: New\n", encoding="utf-8")
        result = adapter.validate_compatibility(sample_expert, str(sandbox))
        assert result["compatible"] is True

    def test_expert_missing_agents(self, adapter, sample_expert, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "README.md").write_text("hi\n", encoding="utf-8")
        result = adapter.validate_compatibility(sample_expert, str(sandbox))
        assert result["compatible"] is False
        assert any("agents" in i for i in result["issues"])


class TestObjectFiles:
    """测试文件管理."""

    def test_get_object_files(self, adapter, sample_skill):
        files = adapter.get_object_files(sample_skill)
        names = [os.path.basename(f) for f in files]
        assert "SKILL.md" in names
        assert "agent.py" in names

    def test_get_files_nonexistent_path(self, adapter):
        obj = ManagedObject(
            object_id="workbuddy:skill:fake",
            platform="workbuddy", object_type="skill", name="fake",
            path=os.path.join(adapter.workbuddy_home, "skills", "fake"),
        )
        assert adapter.get_object_files(obj) == []


class TestWindowsCompat:
    """Windows 路径兼容性 (使用 os.path.join 而非硬编码 '/')."""

    def test_expert_path_uses_os_path_join(self, adapter, sample_expert):
        expected = os.path.normpath(os.path.join(
            adapter.workbuddy_home,
            "plugins", "marketplaces", "my-experts", "plugins", "test-expert",
        ))
        assert os.path.normpath(sample_expert.path) == expected

    def test_staging_path_uses_os_path_join(self, adapter):
        staging = adapter._staging_path("skill", "demo")
        assert os.path.normpath(staging) == os.path.normpath(
            os.path.join(adapter.workbuddy_home, ".ila-staging", "skills", "demo")
        )
        staging_expert = adapter._staging_path("expert", "demo")
        assert os.path.normpath(staging_expert) == os.path.normpath(os.path.join(
            adapter.workbuddy_home, ".ila-staging",
            "plugins", "marketplaces", "my-experts", "plugins", "demo",
        ))
