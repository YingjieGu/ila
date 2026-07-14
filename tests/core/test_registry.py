"""Tests for VersionRegistry."""

import json
import os
import tempfile

import pytest
from ila.core.registry import VersionRegistry
from ila.models.managed_object import ManagedObject


@pytest.fixture
def registry(tmp_path):
    """创建临时注册表."""
    return VersionRegistry(ila_home=str(tmp_path / "ila"))


@pytest.fixture
def sample_object():
    """创建测试对象."""
    return ManagedObject(
        object_id="hermes:skill:test-skill",
        platform="hermes",
        object_type="skill",
        name="test-skill",
        path="/tmp/test-skill",
        current_version="1.0.0",
        metadata={"author": "test"},
    )


class TestVersionRegistryInit:
    """测试注册表初始化."""

    def test_init_creates_db(self, tmp_path):
        reg = VersionRegistry(ila_home=str(tmp_path / "ila"))
        assert os.path.exists(reg.db_path)

    def test_init_creates_tables(self, tmp_path):
        reg = VersionRegistry(ila_home=str(tmp_path / "ila"))
        # 表应该存在
        stats = reg.get_stats()
        assert stats["platforms"] == 0
        assert stats["objects"] == 0


class TestPlatformManagement:
    """测试平台管理."""

    def test_register_and_get(self, registry):
        registry.register_platform("hermes", "HermesAdapter", "~/.hermes", True)
        platforms = registry.get_platforms()
        assert len(platforms) == 1
        assert platforms[0]["platform_id"] == "hermes"
        assert platforms[0]["adapter_class"] == "HermesAdapter"

    def test_register_upsert(self, registry):
        registry.register_platform("hermes", "HermesAdapter")
        registry.register_platform("hermes", "UpdatedAdapter")
        platforms = registry.get_platforms()
        assert len(platforms) == 1
        assert platforms[0]["adapter_class"] == "UpdatedAdapter"


class TestObjectManagement:
    """测试对象管理."""

    def test_register_and_get(self, registry, sample_object):
        registry.register_object(sample_object)
        obj = registry.get_object("hermes:skill:test-skill")
        assert obj is not None
        assert obj["object_id"] == "hermes:skill:test-skill"
        assert obj["current_version"] == "1.0.0"
        assert obj["metadata"]["author"] == "test"

    def test_get_nonexistent(self, registry):
        assert registry.get_object("nonexistent") is None

    def test_get_all_objects(self, registry, sample_object):
        registry.register_object(sample_object)
        obj2 = ManagedObject(
            object_id="hermes:plugin:test-plugin",
            platform="hermes",
            object_type="plugin",
            name="test-plugin",
            path="/tmp/test-plugin",
        )
        registry.register_object(obj2)
        all_objs = registry.get_all_objects()
        assert len(all_objs) == 2

    def test_filter_by_platform(self, registry, sample_object):
        registry.register_object(sample_object)
        obj2 = ManagedObject(
            object_id="openclaw:skill:other",
            platform="openclaw",
            object_type="skill",
            name="other",
            path="/tmp/other",
        )
        registry.register_object(obj2)
        hermes_objs = registry.get_all_objects(platform="hermes")
        assert len(hermes_objs) == 1
        assert hermes_objs[0]["platform"] == "hermes"

    def test_update_version(self, registry, sample_object):
        registry.register_object(sample_object)
        registry.update_object_version("hermes:skill:test-skill", "2.0.0")
        obj = registry.get_object("hermes:skill:test-skill")
        assert obj["current_version"] == "2.0.0"

    def test_delete(self, registry, sample_object):
        registry.register_object(sample_object)
        registry.delete_object("hermes:skill:test-skill")
        assert registry.get_object("hermes:skill:test-skill") is None


class TestVersionManagement:
    """测试版本记录管理."""

    def test_create_version(self, registry, sample_object):
        registry.register_object(sample_object)
        vid = registry.create_version(
            "hermes:skill:test-skill", "1.1.0", "/tmp/sandbox",
            task_spec={"requirement": "fix bug"},
        )
        assert vid > 0
        version = registry.get_version(vid)
        assert version["version"] == "1.1.0"
        assert version["status"] == "developing"
        assert version["task_spec"]["requirement"] == "fix bug"

    def test_update_version_status(self, registry, sample_object):
        registry.register_object(sample_object)
        vid = registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.update_version_status(vid, "testing")
        assert registry.get_version(vid)["status"] == "testing"

    def test_update_version_with_results(self, registry, sample_object):
        registry.register_object(sample_object)
        vid = registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.update_version_status(
            vid, "verified",
            test_results={"verdict": "pass"},
            deploy_verification={"passed": True},
            rollback_snapshot="/tmp/snap.tar.gz",
        )
        version = registry.get_version(vid)
        assert version["status"] == "verified"
        assert version["test_results"]["verdict"] == "pass"
        assert version["rollback_snapshot"] == "/tmp/snap.tar.gz"

    def test_get_versions_by_object(self, registry, sample_object):
        registry.register_object(sample_object)
        registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.create_version("hermes:skill:test-skill", "1.2.0")
        versions = registry.get_versions_by_object("hermes:skill:test-skill")
        assert len(versions) == 2
        # 最新在前
        assert versions[0]["version"] == "1.2.0"

    def test_get_latest_version(self, registry, sample_object):
        registry.register_object(sample_object)
        registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.create_version("hermes:skill:test-skill", "1.2.0")
        latest = registry.get_latest_version("hermes:skill:test-skill")
        assert latest["version"] == "1.2.0"

    def test_get_snapshot_path(self, registry, sample_object):
        registry.register_object(sample_object)
        vid = registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.update_version_status(vid, "live", rollback_snapshot="/tmp/snap1.tar.gz")
        path = registry.get_snapshot_path("hermes:skill:test-skill", "1.1.0")
        assert path == "/tmp/snap1.tar.gz"


class TestTestCases:
    """测试测试用例管理."""

    def test_add_and_get(self, registry, sample_object):
        registry.register_object(sample_object)
        tcid = registry.add_test_case(
            "hermes:skill:test-skill", "functional",
            {"prompt": "hello"}, {"output": "world"},
        )
        assert tcid > 0
        cases = registry.get_test_cases("hermes:skill:test-skill")
        assert len(cases) == 1
        assert cases[0]["test_input"]["prompt"] == "hello"
        assert cases[0]["expected_output"]["output"] == "world"

    def test_filter_by_type(self, registry, sample_object):
        registry.register_object(sample_object)
        registry.add_test_case("hermes:skill:test-skill", "functional", {"prompt": "a"})
        registry.add_test_case("hermes:skill:test-skill", "regression", {"prompt": "b"})
        functional = registry.get_test_cases("hermes:skill:test-skill", "functional")
        assert len(functional) == 1
        assert functional[0]["test_type"] == "functional"


class TestSelfEvolution:
    """测试 ILA 自迭代版本管理."""

    def test_create_self_version(self, registry):
        vid = registry.create_self_version("1.1.0", "优化测试速度", "/tmp/sandbox")
        assert vid > 0

    def test_update_self_status(self, registry):
        vid = registry.create_self_version("1.1.0", "test")
        registry.update_self_version_status(vid, "live", "approved")
        versions = registry.get_self_versions()
        assert versions[0]["status"] == "live"
        assert versions[0]["review_status"] == "approved"


class TestStats:
    """测试统计."""

    def test_stats(self, registry, sample_object):
        registry.register_platform("hermes", "HermesAdapter")
        registry.register_object(sample_object)
        registry.create_version("hermes:skill:test-skill", "1.1.0")
        registry.add_test_case("hermes:skill:test-skill", "functional", {"prompt": "test"})
        stats = registry.get_stats()
        assert stats["platforms"] == 1
        assert stats["objects"] == 1
        assert stats["total_versions"] == 1
        assert stats["test_cases"] == 1
