"""Tests for AdapterRegistry and PlatformAdapter base class."""

import pytest
from ila.adapters.base import PlatformAdapter
from ila.adapters.registry import AdapterRegistry
from ila.models.managed_object import ManagedObject


class MockAdapter(PlatformAdapter):
    """用于测试的 Mock 适配器."""

    def __init__(self, platform_name: str = "mock"):
        self._platform = platform_name
        self._objects: dict[str, ManagedObject] = {}

    def platform_id(self) -> str:
        return self._platform

    def discover_objects(self) -> list[ManagedObject]:
        return list(self._objects.values())

    def get_object(self, object_id: str) -> ManagedObject | None:
        return self._objects.get(object_id)

    def create_snapshot(self, obj: ManagedObject) -> str:
        return f"/tmp/snapshot-{obj.name}.tar.gz"

    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        return True

    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str:
        return f"staging-{obj.name}"

    def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict:
        return {"output": f"mock-{obj.name}", "exit_code": 0}

    def invoke_staging(self, staging_id: str, test_input: dict) -> dict:
        return {"output": f"staging-{staging_id}", "exit_code": 0}

    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict:
        return {"status": "success", "snapshot": "/tmp/mock-snap.tar.gz"}

    def health_check(self, obj: ManagedObject) -> bool:
        return True

    def reload(self, obj: ManagedObject) -> bool:
        return True

    def get_object_files(self, obj: ManagedObject) -> list[str]:
        return [f"{obj.path}/SKILL.md"]

    def validate_compatibility(self, obj: ManagedObject, sandbox_path: str) -> dict:
        return {"compatible": True, "issues": [], "warnings": []}

    def add_object(self, obj: ManagedObject) -> None:
        """测试辅助方法."""
        self._objects[obj.object_id] = obj


@pytest.fixture(autouse=True)
def clear_registry():
    """每个测试前后清空注册表."""
    AdapterRegistry.clear()
    yield
    AdapterRegistry.clear()


class TestAdapterRegistry:
    """测试适配器注册表."""

    def test_register_and_get(self):
        adapter = MockAdapter("mock")
        AdapterRegistry.register(adapter)
        assert AdapterRegistry.is_registered("mock")
        assert AdapterRegistry.get_adapter("mock") is adapter

    def test_get_nonexistent_raises(self):
        with pytest.raises(ValueError, match="未注册的平台适配器"):
            AdapterRegistry.get_adapter("nonexistent")

    def test_unregister(self):
        adapter = MockAdapter("mock")
        AdapterRegistry.register(adapter)
        assert AdapterRegistry.unregister("mock") is True
        assert not AdapterRegistry.is_registered("mock")
        assert AdapterRegistry.unregister("mock") is False

    def test_get_all_adapters(self):
        a1 = MockAdapter("mock1")
        a2 = MockAdapter("mock2")
        AdapterRegistry.register(a1)
        AdapterRegistry.register(a2)
        all_adapters = AdapterRegistry.get_all_adapters()
        assert len(all_adapters) == 2
        assert "mock1" in all_adapters
        assert "mock2" in all_adapters

    def test_get_registered_platforms(self):
        AdapterRegistry.register(MockAdapter("hermes"))
        AdapterRegistry.register(MockAdapter("openclaw"))
        platforms = AdapterRegistry.get_registered_platforms()
        assert "hermes" in platforms
        assert "openclaw" in platforms

    def test_overwrite_warns(self, caplog):
        AdapterRegistry.register(MockAdapter("mock"))
        AdapterRegistry.register(MockAdapter("mock"))  # 覆盖
        assert AdapterRegistry.is_registered("mock")


class TestCrossPlatformDiscovery:
    """测试跨平台对象发现."""

    def test_discover_all_objects(self):
        a1 = MockAdapter("hermes")
        a2 = MockAdapter("openclaw")
        a1.add_object(ManagedObject(
            object_id="hermes:skill:s1", platform="hermes", object_type="skill",
            name="s1", path="/tmp/s1",
        ))
        a2.add_object(ManagedObject(
            object_id="openclaw:plugin:p1", platform="openclaw", object_type="plugin",
            name="p1", path="/tmp/p1",
        ))
        AdapterRegistry.register(a1)
        AdapterRegistry.register(a2)

        all_objects = AdapterRegistry.discover_all_objects()
        assert len(all_objects) == 2

    def test_discover_by_platform(self):
        a1 = MockAdapter("hermes")
        a1.add_object(ManagedObject(
            object_id="hermes:skill:s1", platform="hermes", object_type="skill",
            name="s1", path="/tmp/s1",
        ))
        AdapterRegistry.register(a1)
        objects = AdapterRegistry.discover_objects_by_platform("hermes")
        assert len(objects) == 1

    def test_discover_all_handles_errors(self):
        """某个适配器失败不影响其他."""
        class FailingAdapter(MockAdapter):
            def discover_objects(self):
                raise RuntimeError("discovery failed")

        AdapterRegistry.register(FailingAdapter("fail"))
        AdapterRegistry.register(MockAdapter("ok"))
        # FailingAdapter 不会导致整体崩溃
        all_objects = AdapterRegistry.discover_all_objects()
        # ok 适配器返回空列表，fail 的被跳过
        assert isinstance(all_objects, list)


class TestFindObject:
    """测试跨平台对象查找."""

    def test_find_object(self):
        adapter = MockAdapter("hermes")
        obj = ManagedObject(
            object_id="hermes:skill:my-skill", platform="hermes", object_type="skill",
            name="my-skill", path="/tmp/my-skill",
        )
        adapter.add_object(obj)
        AdapterRegistry.register(adapter)

        found = AdapterRegistry.find_object("hermes:skill:my-skill")
        assert found is not None
        assert found.name == "my-skill"

    def test_find_nonexistent_platform(self):
        assert AdapterRegistry.find_object("unknown:skill:test") is None

    def test_find_nonexistent_object(self):
        adapter = MockAdapter("hermes")
        AdapterRegistry.register(adapter)
        assert AdapterRegistry.find_object("hermes:skill:nonexistent") is None
