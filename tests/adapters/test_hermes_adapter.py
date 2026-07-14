"""Tests for HermesAdapter."""

import os
import tarfile
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from ila.adapters.hermes_adapter import HermesAdapter
from ila.models.managed_object import ManagedObject


@pytest.fixture
def hermes_home(tmp_path):
    """创建临时 Hermes home 目录结构."""
    home = tmp_path / "hermes"
    # Skills
    skill_dir = home / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\nversion: 1.0.0\n---\n# Test Skill\n"
    )
    (skill_dir / "handler.py").write_text("def handle(): pass\n")

    # Skill with category
    cat_skill_dir = home / "skills" / "category1" / "sub-skill"
    cat_skill_dir.mkdir(parents=True)
    (cat_skill_dir / "SKILL.md").write_text(
        "---\nname: sub-skill\nversion: 2.1.0\n---\n# Sub Skill\n"
    )

    # Plugins
    plugin_dir = home / "plugins" / "test-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("# plugin\n")

    # Profiles
    profile_dir = home / "profiles" / "test-profile"
    profile_dir.mkdir(parents=True)

    # Config
    (home / "config.yaml").write_text(
        "mcp:\n  servers:\n    test-server:\n      command: node\n      args: ['server.js']\n"
    )

    return str(home)


@pytest.fixture
def adapter(hermes_home):
    """创建适配器实例."""
    return HermesAdapter(hermes_home=hermes_home, staging_profile="ila-test")


@pytest.fixture
def sample_skill(hermes_home):
    """创建测试 skill 对象."""
    return ManagedObject(
        object_id="hermes:skill:test-skill",
        platform="hermes",
        object_type="skill",
        name="test-skill",
        path=os.path.join(hermes_home, "skills", "test-skill"),
        current_version="1.0.0",
    )


class TestDiscovery:
    """测试对象发现."""

    def test_platform_id(self, adapter):
        assert adapter.platform_id() == "hermes"

    def test_discover_skills(self, adapter):
        objects = adapter.discover_objects()
        skill_names = [o.name for o in objects if o.object_type == "skill"]
        assert "test-skill" in skill_names
        assert "sub-skill" in skill_names

    def test_discover_skill_version(self, adapter):
        objects = adapter.discover_objects()
        skill = next(o for o in objects if o.name == "test-skill")
        assert skill.current_version == "1.0.0"

    def test_discover_plugins(self, adapter):
        objects = adapter.discover_objects()
        plugin_names = [o.name for o in objects if o.object_type == "plugin"]
        assert "test-plugin" in plugin_names

    def test_discover_profiles(self, adapter):
        objects = adapter.discover_objects()
        profile_names = [o.name for o in objects if o.object_type == "agent"]
        assert "test-profile" in profile_names

    def test_discover_mcp_servers(self, adapter):
        objects = adapter.discover_objects()
        mcp_names = [o.name for o in objects if o.object_type == "mcp"]
        assert "test-server" in mcp_names

    def test_get_object(self, adapter):
        obj = adapter.get_object("hermes:skill:test-skill")
        assert obj is not None
        assert obj.name == "test-skill"
        assert obj.current_version == "1.0.0"

    def test_get_object_nonexistent(self, adapter):
        assert adapter.get_object("hermes:skill:nonexistent") is None

    def test_read_skill_version(self, adapter, hermes_home):
        path = os.path.join(hermes_home, "skills", "test-skill")
        assert adapter._read_skill_version(path) == "1.0.0"

    def test_read_skill_version_unknown(self, adapter, tmp_path):
        """没有 SKILL.md 时返回 unknown."""
        assert adapter._read_skill_version(str(tmp_path)) == "unknown"


class TestSnapshot:
    """测试快照与恢复."""

    def test_create_snapshot(self, adapter, sample_skill):
        snapshot_path = adapter.create_snapshot(sample_skill)
        assert os.path.exists(snapshot_path)
        assert snapshot_path.endswith(".tar.gz")

        # 验证内容
        with tarfile.open(snapshot_path, "r:gz") as tar:
            names = tar.getnames()
            assert "test-skill/SKILL.md" in names or "test-skill" in names

    def test_restore_snapshot(self, adapter, sample_skill, hermes_home):
        # 创建快照
        snapshot_path = adapter.create_snapshot(sample_skill)

        # 修改原文件
        skill_path = sample_skill.path
        with open(os.path.join(skill_path, "handler.py"), "w") as f:
            f.write("# modified\n")

        # 恢复
        result = adapter.restore_snapshot(sample_skill, snapshot_path)
        assert result is True

        # 验证恢复
        with open(os.path.join(skill_path, "handler.py")) as f:
            content = f.read()
        assert content == "def handle(): pass\n"

    def test_restore_nonexistent_snapshot(self, adapter, sample_skill):
        result = adapter.restore_snapshot(sample_skill, "/tmp/nonexistent.tar.gz")
        assert result is False


class TestObjectFiles:
    """测试文件管理."""

    def test_get_object_files(self, adapter, sample_skill):
        files = adapter.get_object_files(sample_skill)
        assert len(files) >= 2  # SKILL.md + handler.py
        names = [os.path.basename(f) for f in files]
        assert "SKILL.md" in names
        assert "handler.py" in names

    def test_get_files_nonexistent_path(self, adapter):
        obj = ManagedObject(
            object_id="hermes:skill:fake", platform="hermes",
            object_type="skill", name="fake", path="/tmp/nonexistent-xyz",
        )
        files = adapter.get_object_files(obj)
        assert files == []


class TestCompatibility:
    """测试兼容性验证."""

    def test_compatible(self, adapter, sample_skill, tmp_path):
        # 创建一个兼容的新版本
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 1.1.0\n---\n# Test Skill v1.1\n"
        )
        (sandbox / "handler.py").write_text("def handle(): return 'ok'\n")

        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is True
        assert len(result["issues"]) == 0

    def test_missing_skill_md(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "handler.py").write_text("# only handler\n")

        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is False
        assert any("SKILL.md" in issue for issue in result["issues"])

    def test_broken_frontmatter(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text("---\nno closing frontmatter\n# Skill\n")
        (sandbox / "handler.py").write_text("def handle(): pass\n")

        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is False

    def test_missing_files_warning(self, adapter, sample_skill, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 1.1.0\n---\n# Test\n"
        )
        # 缺少 handler.py

        result = adapter.validate_compatibility(sample_skill, str(sandbox))
        assert result["compatible"] is True  # warnings 不阻止
        assert any("handler.py" in w or "缺少文件" in w for w in result["warnings"])


class TestHotSwap:
    """测试热切换 (使用 mock 避免实际 Hermes 调用)."""

    @patch.object(HermesAdapter, "reload", return_value=True)
    @patch.object(HermesAdapter, "health_check", return_value=True)
    def test_hot_swap_success(self, mock_health, mock_reload, adapter, sample_skill, tmp_path):
        # 创建新版本沙箱
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text(
            "---\nname: test-skill\nversion: 1.1.0\n---\n# Updated\n"
        )
        (sandbox / "handler.py").write_text("def handle(): return 'new'\n")

        result = adapter.hot_swap(sample_skill, str(sandbox))
        assert result["status"] == "success"
        assert "snapshot" in result

        # 验证新版本已替换
        with open(os.path.join(sample_skill.path, "SKILL.md")) as f:
            content = f.read()
        assert "1.1.0" in content

    @patch.object(HermesAdapter, "reload", return_value=True)
    @patch.object(HermesAdapter, "health_check", return_value=False)
    def test_hot_swap_rollback_on_health_fail(self, mock_health, mock_reload,
                                               adapter, sample_skill, tmp_path):
        # 保存原始内容
        original_path = sample_skill.path
        with open(os.path.join(original_path, "SKILL.md")) as f:
            original_content = f.read()

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "SKILL.md").write_text("---\nname: test-skill\nversion: 9.0.0\n---\n# Bad\n")

        result = adapter.hot_swap(sample_skill, str(sandbox))
        assert result["status"] == "rolled_back"
        assert "health check" in result["reason"]

        # 验证恢复到原始内容
        with open(os.path.join(original_path, "SKILL.md")) as f:
            restored_content = f.read()
        assert restored_content == original_content


class TestInvoke:
    """测试对象调用 (使用 mock)."""

    @patch("subprocess.run")
    def test_invoke_object(self, mock_run, adapter, sample_skill):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="OK\n", stderr=""
        )
        result = adapter.invoke_object(sample_skill, {"prompt": "hello"})
        assert result["output"] == "OK"
        assert result["exit_code"] == 0

    @patch("subprocess.run")
    def test_invoke_staging(self, mock_run, adapter):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="staging result\n", stderr=""
        )
        result = adapter.invoke_staging(
            "staging-001", {"skill": "test-skill", "prompt": "test"}
        )
        assert "staging result" in result["output"]
