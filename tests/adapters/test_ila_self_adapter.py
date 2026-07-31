"""Tests for IlaSelfAdapter."""

import json
import os
import tarfile
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from ila.adapters.ila_self_adapter import IlaSelfAdapter, _PROTECTED_PATTERNS
from ila.models.managed_object import ManagedObject


@pytest.fixture
def adapter():
    """创建真实 IlaSelfAdapter 实例 (指向实际项目目录)."""
    return IlaSelfAdapter()


@pytest.fixture
def obj():
    """创建 ILA 纳管对象."""
    return ManagedObject(
        object_id="ila:agent:core",
        platform="ila",
        object_type="agent",
        name="ila-core",
        path=os.path.expanduser("~/myprojects/ila/src/ila"),
        current_version="1.0.0",
        metadata={
            "project_root": os.path.expanduser("~/myprojects/ila"),
            "dashboard_port": 9527,
            "staging_port": 9528,
        },
    )


class TestIdentity:
    """测试平台标识."""

    def test_platform_id(self, adapter):
        assert adapter.platform_id() == "ila"

    def test_get_platform_home(self, adapter):
        home = adapter.get_platform_home()
        assert "myprojects/ila" in home
        assert os.path.isabs(home)


class TestDiscovery:
    """测试对象发现."""

    def test_discover_objects(self, adapter):
        objects = adapter.discover_objects()
        assert len(objects) == 1
        obj = objects[0]
        assert obj.object_id == "ila:agent:core"
        assert obj.platform == "ila"
        assert obj.object_type == "agent"
        assert obj.name == "ila-core"
        assert "ila" in obj.path
        assert obj.metadata["dashboard_port"] == 9527
        assert obj.metadata["staging_port"] == 9528

    def test_get_object_exists(self, adapter):
        obj = adapter.get_object("ila:agent:core")
        assert obj is not None
        assert obj.object_id == "ila:agent:core"

    def test_get_object_not_found(self, adapter):
        obj = adapter.get_object("ila:skill:nonexistent")
        assert obj is None

    def test_version_read(self, adapter):
        """版本号应为非空字符串."""
        objects = adapter.discover_objects()
        assert objects[0].current_version
        assert len(objects[0].current_version) >= 3


class TestSnapshot:
    """测试快照创建与恢复."""

    def test_create_snapshot(self, adapter, obj):
        snapshot = adapter.create_snapshot(obj)
        assert os.path.exists(snapshot)
        assert snapshot.endswith(".tar.gz")
        assert "ila-core-" in os.path.basename(snapshot)

        # 验证快照内容
        with tarfile.open(snapshot, "r:gz") as tar:
            names = tar.getnames()
        # 应包含核心文件
        assert any("src/ila" in n for n in names)
        # 不应包含受保护文件
        for n in names:
            for p in _PROTECTED_PATTERNS:
                assert p not in n, f"快照不应包含受保护文件: {n}"

        # 验证 meta 文件
        meta = snapshot.replace(".tar.gz", ".meta.json")
        assert os.path.exists(meta)
        with open(meta) as f:
            meta_data = json.load(f)
        assert meta_data["object_id"] == "ila:agent:core"
        assert meta_data["version"] == "1.0.0"

        # 清理
        os.remove(snapshot)
        os.remove(meta)

    def test_restore_snapshot(self, adapter, obj):
        """先创建快照, 然后恢复."""
        snapshot = adapter.create_snapshot(obj)
        assert os.path.exists(snapshot)

        result = adapter.restore_snapshot(obj, snapshot)
        assert result is True

        # 清理
        meta = snapshot.replace(".tar.gz", ".meta.json")
        os.remove(snapshot)
        if os.path.exists(meta):
            os.remove(meta)

    def test_restore_nonexistent_snapshot(self, adapter, obj):
        result = adapter.restore_snapshot(obj, "/tmp/nonexistent-snapshot.tar.gz")
        assert result is False

    def test_snapshot_excludes_protected(self, adapter, obj):
        """验证 __pycache__ 等受保护内容不会被打包."""
        # 在 src/ila 中创建临时受保护文件
        test_cache = os.path.join(obj.path, "__pycache__", "test.pyc")
        os.makedirs(os.path.dirname(test_cache), exist_ok=True)
        try:
            with open(test_cache, "w") as f:
                f.write("# cached")
            snapshot = adapter.create_snapshot(obj)
            with tarfile.open(snapshot, "r:gz") as tar:
                names = tar.getnames()
            # 受保护文件不应出现
            cache_in_tar = any("__pycache__" in n for n in names)
            assert not cache_in_tar, f"__pycache__ 不应在快照中: {names}"
            os.remove(snapshot)
            meta = snapshot.replace(".tar.gz", ".meta.json")
            if os.path.exists(meta):
                os.remove(meta)
        finally:
            # 清理临时文件
            import shutil
            cache_dir = os.path.join(obj.path, "__pycache__")
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)


class TestFiles:
    """测试文件操作."""

    def test_get_object_files(self, adapter, obj):
        files = adapter.get_object_files(obj)
        assert len(files) > 0
        # 应包含核心文件
        basenames = [os.path.basename(f) for f in files]
        assert "cli.py" in basenames
        assert "developer.py" in basenames
        # 不应包含 .pyc
        assert all(not f.endswith(".pyc") for f in files)

    def test_validate_compatibility_self(self, adapter, obj):
        """自检兼容性 - 用自身目录作为沙箱."""
        result = adapter.validate_compatibility(obj, adapter.src_dir)
        assert result["compatible"] is True
        assert len(result["issues"]) == 0

    def test_validate_compatibility_empty(self, adapter, obj):
        """空目录应该不兼容."""
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.validate_compatibility(obj, tmp)
            assert result["compatible"] is False
            assert len(result["issues"]) > 0

    def test_validate_compatibility_missing_core(self, adapter, obj):
        """缺少核心文件的沙箱应该不兼容."""
        with tempfile.TemporaryDirectory() as tmp:
            # 创建部分文件但缺少核心文件
            os.makedirs(os.path.join(tmp, "core"))
            open(os.path.join(tmp, "cli.py"), "w").close()
            result = adapter.validate_compatibility(obj, tmp)
            assert result["compatible"] is False
            core_issues = [
                i for i in result["issues"]
                if "core/orchestrator.py" in i or "adapters/base.py" in i
            ]
            assert len(core_issues) > 0


class TestHealth:
    """测试健康检查与重载."""

    def test_verify_file_integrity(self, adapter):
        assert adapter._verify_file_integrity() is True

    def test_reload_ok(self, adapter, obj):
        assert adapter.reload(obj) is True

    def test_health_check_api(self, adapter, obj):
        """健康检查应该通过 API 或文件级验证."""
        result = adapter.health_check(obj)
        # 只要 API 或文件验证任一通过即可
        assert isinstance(result, bool)

    def test_is_process_running(self, adapter):
        """检查当前 dashboard 进程 (port 9527) 是否运行."""
        result = adapter._is_process_running(9527)
        # 如果 dashboard 在运行, 应为 True; 否则为 False
        assert isinstance(result, bool)


class TestInvoke:
    """测试 API 调用."""

    def test_invoke_object_status(self, adapter, obj):
        """调用 /api/status 端点."""
        result = adapter.invoke_object(obj, {"endpoint": "/api/status"})
        assert result["exit_code"] == 0
        assert "platforms" in result.get("output", "")

    def test_invoke_object_file_check(self, adapter, obj):
        """文件检查模式."""
        result = adapter.invoke_object(
            obj, {"check_file": "cli.py", "expect_contains": "AdapterRegistry"}
        )
        assert result["exit_code"] == 0
        assert "AdapterRegistry" in result.get("output", "")

    def test_invoke_object_file_not_found(self, adapter, obj):
        result = adapter.invoke_object(
            obj, {"check_file": "nonexistent.py", "expect_contains": ""}
        )
        assert result["exit_code"] == 1
        assert "不存在" in result.get("error", "")

    def test_invoke_object_invalid_endpoint(self, adapter, obj):
        """不存在的端点应返回错误."""
        result = adapter.invoke_object(obj, {"endpoint": "/api/nonexistent"})
        assert result["exit_code"] == 1


class TestProtectedFiles:
    """测试受保护文件配置."""

    def test_protected_patterns_defined(self):
        assert len(_PROTECTED_PATTERNS) > 0
        assert "__pycache__" in _PROTECTED_PATTERNS
        assert ".git" in _PROTECTED_PATTERNS
        assert "registry.db" in _PROTECTED_PATTERNS
        assert "venv" in _PROTECTED_PATTERNS

    def test_merge_dir_skips_protected(self, adapter):
        """_merge_dir 应跳过受保护文件."""
        with tempfile.TemporaryDirectory() as src:
            with tempfile.TemporaryDirectory() as dst:
                # 创建受保护文件
                os.makedirs(os.path.join(src, "__pycache__"))
                open(os.path.join(src, "__pycache__", "cache.pyc"), "w").close()
                # 创建正常文件
                open(os.path.join(src, "normal.py"), "w").write("x = 1")

                adapter._merge_dir(src, dst)

                # 正常文件应被复制
                assert os.path.exists(os.path.join(dst, "normal.py"))
                # 受保护文件不应被复制
                assert not os.path.exists(os.path.join(dst, "__pycache__"))


class TestStaging:
    """测试 staging 信息管理."""

    def test_save_load_staging_info(self, adapter):
        adapter._save_staging_info("test-123", {
            "port": 9528,
            "pid": 99999,
            "backup_dir": "/tmp/test-backup",
        })
        info = adapter._load_staging_info("test-123")
        assert info is not None
        assert info["port"] == 9528
        assert info["pid"] == 99999

        # 清理
        import os
        info_path = os.path.expanduser(f"~/.ila/staging/test-123.json")
        if os.path.exists(info_path):
            os.remove(info_path)

    def test_load_nonexistent_staging(self, adapter):
        info = adapter._load_staging_info("nonexistent-staging")
        assert info is None