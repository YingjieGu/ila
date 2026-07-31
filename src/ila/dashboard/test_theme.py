"""主题切换功能测试.

覆盖:
- 主题目录常量与助手函数 (resolve_theme / get_themes / is_valid_theme)
- REST 端点 GET /api/themes、POST /api/theme
- 配置项 dashboard.theme 作为初始主题
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ila.dashboard import (
    AVAILABLE_THEMES,
    DEFAULT_THEME,
    DEFAULT_PAGE_SIZE,
    get_themes,
    is_valid_theme,
    resolve_theme,
)
from ila.dashboard.api import create_app


# ---- 主题目录与助手函数 ----

def test_available_themes_contains_core_options():
    assert "dark" in AVAILABLE_THEMES
    assert "light" in AVAILABLE_THEMES
    assert DEFAULT_THEME in AVAILABLE_THEMES


def test_get_themes_returns_id_and_name():
    themes = get_themes()
    assert isinstance(themes, list)
    assert len(themes) == len(AVAILABLE_THEMES)
    for item in themes:
        assert set(item.keys()) == {"id", "name"}
        assert item["id"] in AVAILABLE_THEMES
        assert item["name"]  # 名称非空


def test_resolve_theme_valid_passthrough():
    for theme in AVAILABLE_THEMES:
        assert resolve_theme(theme) == theme


def test_resolve_theme_invalid_falls_back_to_default():
    assert resolve_theme("nope") == DEFAULT_THEME
    assert resolve_theme(None) == DEFAULT_THEME
    assert resolve_theme("") == DEFAULT_THEME


def test_is_valid_theme_boolean():
    assert is_valid_theme("dark") is True
    assert is_valid_theme("ocean") is True
    assert is_valid_theme("invalid") is False
    assert is_valid_theme("") is False
    assert is_valid_theme(None) is False


def test_grassland_theme_supported():
    """草原 (grassland) 主题应在目录中，并具备中文展示名."""
    assert "grassland" in AVAILABLE_THEMES
    assert is_valid_theme("grassland") is True
    assert resolve_theme("grassland") == "grassland"

    themes = get_themes()
    grassland = next((t for t in themes if t["id"] == "grassland"), None)
    assert grassland is not None
    assert grassland["name"] == "草原"


# ---- REST 端点 ----

@pytest.fixture()
def client(tmp_path):
    """以临时 ila_home 构建应用，初始主题为 light."""
    config = {"ila": {"home": str(tmp_path)}, "dashboard": {"theme": "light"}}
    app = create_app(config, sandbox_manager=None)
    return TestClient(app)


def test_list_themes_endpoint(client):
    res = client.get("/api/themes")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data["themes"], list)
    assert {"id": "dark", "name": "暗色"} in data["themes"]
    assert data["default"] == DEFAULT_THEME
    # 初始主题来自 config["dashboard"]["theme"]
    assert data["current"] == "light"


def test_set_theme_valid(client):
    res = client.post("/api/theme", json={"theme": "ocean"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["current"] == "ocean"
    # 后续读取应反映新主题
    assert client.get("/api/themes").json()["current"] == "ocean"


def test_set_theme_grassland(client):
    """通过 REST 端点切换到草原主题，并校验后续读取一致."""
    res = client.post("/api/theme", json={"theme": "grassland"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["current"] == "grassland"
    assert client.get("/api/themes").json()["current"] == "grassland"


def test_set_theme_invalid_returns_error(client):
    res = client.post("/api/theme", json={"theme": "purple"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "error"
    assert "dark" in body["supported"]
    # 当前主题不应改变
    assert client.get("/api/themes").json()["current"] == "light"


def test_set_theme_persists_across_calls(client):
    assert client.post("/api/theme", json={"theme": "sepia"}).json()["current"] == "sepia"
    assert client.post("/api/theme", json={"theme": "dark"}).json()["current"] == "dark"
    assert client.get("/api/themes").json()["current"] == "dark"


def test_default_theme_when_config_missing(tmp_path):
    """未配置 dashboard.theme 时，初始主题应为默认主题."""
    config = {"ila": {"home": str(tmp_path)}}
    app = create_app(config, sandbox_manager=None)
    client = TestClient(app)
    assert client.get("/api/themes").json()["current"] == DEFAULT_THEME


# ---- 纳管对象分页 ----

def test_default_page_size_constant():
    """DEFAULT_PAGE_SIZE 应为正整数."""
    assert DEFAULT_PAGE_SIZE == 10
    assert isinstance(DEFAULT_PAGE_SIZE, int)
    assert DEFAULT_PAGE_SIZE > 0


def test_objects_endpoint_returns_paginated_structure(client):
    """GET /api/objects 返回的 JSON 应包含分页字段."""
    res = client.get("/api/objects")
    assert res.status_code == 200
    data = res.json()
    assert "objects" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "total_pages" in data
    assert data["page"] == 1
    assert data["page_size"] == DEFAULT_PAGE_SIZE
    assert data["total_pages"] >= 1


def test_objects_endpoint_page_param(client, monkeypatch):
    """GET /api/objects?page=N 应正确分页."""
    from ila.core.orchestrator import ILAOrchestrator

    # Patch discover to return 25 fake objects
    def fake_discover(self, platform=None):
        return [{"object_id": f"test:skill:obj{i:03d}", "object_type": "skill",
                 "current_version": "v1"} for i in range(25)]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)

    # Page 1: 应有 10 条
    res1 = client.get("/api/objects?page=1&page_size=10")
    data1 = res1.json()
    assert data1["page"] == 1
    assert len(data1["objects"]) == 10
    assert data1["total"] == 25
    assert data1["total_pages"] == 3

    # Page 2: 应有 10 条
    res2 = client.get("/api/objects?page=2&page_size=10")
    data2 = res2.json()
    assert data2["page"] == 2
    assert len(data2["objects"]) == 10

    # Page 3: 应有 5 条 (最后一页)
    res3 = client.get("/api/objects?page=3&page_size=10")
    data3 = res3.json()
    assert data3["page"] == 3
    assert len(data3["objects"]) == 5


def test_objects_endpoint_custom_page_size(client, monkeypatch):
    """GET /api/objects?page_size=N 应使用自定义分页大小."""
    from ila.core.orchestrator import ILAOrchestrator

    def fake_discover(self, platform=None):
        return [{"object_id": f"test:skill:obj{i:03d}", "object_type": "skill",
                 "current_version": "v1"} for i in range(20)]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)

    res = client.get("/api/objects?page_size=5")
    data = res.json()
    assert data["page_size"] == 5
    assert len(data["objects"]) == 5
    assert data["total_pages"] == 4


def test_objects_endpoint_page_out_of_range_clamped(client, monkeypatch):
    """超出范围的 page 参数应被钳制到有效范围."""
    from ila.core.orchestrator import ILAOrchestrator

    def fake_discover(self, platform=None):
        return [{"object_id": f"test:skill:obj{i:03d}", "object_type": "skill",
                 "current_version": "v1"} for i in range(5)]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)

    # page=0 -> clamped to 1
    res = client.get("/api/objects?page=0&page_size=10")
    data = res.json()
    assert data["page"] == 1
    assert len(data["objects"]) == 5

    # page=999 -> clamped to total_pages
    res = client.get("/api/objects?page=999&page_size=10")
    data = res.json()
    assert data["page"] == 1
    assert len(data["objects"]) == 5


def test_objects_endpoint_empty(client):
    """无对象时仍返回正确的分页结构."""
    res = client.get("/api/objects")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["total_pages"] == 1
    assert data["objects"] == []


# ---- 版本历史操作 (按状态映射可用操作) ----

from ila import (
    VERSION_OPERATIONS,
    get_operation_label,
    get_operation_target_status,
    get_version_operations,
    is_valid_version_operation,
)
from ila.core.registry import VersionRegistry
from ila.models.managed_object import ManagedObject

OBJ_ID = "test:skill:obj"


def _seed_version(tmp_path, object_id=OBJ_ID, version="v1", status="developing", task_spec=None):
    """在临时 ila_home 中注册对象并创建一条指定状态的版本记录."""
    reg = VersionRegistry(ila_home=str(tmp_path))
    reg.register_object(ManagedObject(
        object_id=object_id, platform="test", object_type="skill",
        name="obj", path="/tmp/obj", current_version="v0",
    ))
    vid = reg.create_version(object_id, version, sandbox_path="/tmp/sb", task_spec=task_spec)
    if status != "developing":
        reg.update_version_status(vid, status)
    return vid


def test_version_operations_maps_status_to_ops():
    """每个状态映射到非空操作列表，且操作值合法."""
    for status, ops in VERSION_OPERATIONS.items():
        assert isinstance(ops, (list, tuple)) and ops, f"{status} 应至少有一个操作"
        for op in ops:
            assert is_valid_version_operation(op), f"{status} 含非法操作 {op}"


def test_get_version_operations_known_and_unknown():
    assert "rollback" in get_version_operations("live")
    assert get_version_operations("nope") == []


def test_operation_label_and_target_status():
    assert get_operation_label("rollback")
    assert get_operation_target_status("rollback") == "rolled_back"
    # 未知操作: 标签回退为操作名本身，目标状态为空串
    assert get_operation_label("unknown") == "unknown"
    assert get_operation_target_status("unknown") == ""


def test_is_valid_version_operation_boolean():
    assert is_valid_version_operation("rollback") is True
    assert is_valid_version_operation("deploy_verify") is True
    assert is_valid_version_operation("stop") is True
    assert is_valid_version_operation("iterate") is True
    assert is_valid_version_operation("nope") is False


def test_list_version_operations_endpoint(client):
    res = client.get("/api/version/operations")
    assert res.status_code == 200
    data = res.json()
    assert "operations" in data
    ops = data["operations"]
    # live 状态应暴露 rollback 操作
    live_ops = [o["operation"] for o in ops.get("live", [])]
    assert "rollback" in live_ops
    # 每个操作条目包含 label 与 target_status
    for status_items in ops.values():
        for item in status_items:
            assert set(item.keys()) == {"operation", "label", "target_status"}
            assert item["label"]
            assert item["target_status"]


def test_operate_deploy_verify(client, tmp_path):
    vid = _seed_version(tmp_path, status="testing")
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "deploy_verify"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["version_status"] == "verified"


def test_operate_stop(client, tmp_path):
    vid = _seed_version(tmp_path, status="developing")
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "stop"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["version_status"] == "stopped"


def test_operate_iterate_without_requirement(client, tmp_path):
    """重新迭代: 无 task_spec.requirement 时仅切回 developing，不触发后台闭环."""
    vid = _seed_version(tmp_path, status="failed")
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "iterate"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["version_status"] == "developing"
    # run 为 None 表示未触发后台闭环
    assert body.get("run") is None


def test_operate_rollback_delegates_to_orchestrator(client, tmp_path, monkeypatch):
    """rollback 操作应委托给 orchestrator.rollback 并透传其结果."""
    vid = _seed_version(tmp_path, status="live")
    app = client.app
    monkeypatch.setattr(
        app.state.orchestrator, "rollback",
        lambda object_id: {"status": "success", "object_id": object_id},
    )
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "rollback"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["object_id"] == OBJ_ID


def test_operate_invalid_operation(client, tmp_path):
    vid = _seed_version(tmp_path, status="developing")
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "nope"})
    assert res.status_code == 200
    assert res.json()["status"] == "error"


def test_operate_disallowed_for_status(client, tmp_path):
    """对 live 版本执行 stop (非允许操作) 应被拒绝."""
    vid = _seed_version(tmp_path, status="live")
    res = client.post(f"/api/version/{vid}/operate", json={"operation": "stop"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "error"
    assert "allowed" in body


def test_operate_missing_version(client):
    res = client.post("/api/version/9999/operate", json={"operation": "stop"})
    assert res.status_code == 200
    assert res.json()["status"] == "error"


# ---- 版本历史操作按钮配色 (蓝色主操作按钮) ----

def test_version_history_op_buttons_are_blue(client):
    """版本历史板块的迭代记录操作按钮应使用蓝色主操作样式 (btn-primary).

    渲染模板里 opButtons 不应再使用中性灰的内联样式，而应沿用 btn-primary。
    """
    html = client.get("/").text
    # 定位版本历史操作按钮的渲染片段 (含 onclick=operateVersion 的 button)
    start = html.find('onclick="operateVersion(${v.id}')
    assert start != -1, "版本历史操作按钮渲染片段缺失"
    frag = html[start - 200:start + 120]
    # 蓝色主操作按钮类名应出现在该片段上下文中
    assert "btn btn-primary btn-sm" in frag
    # 中性灰内联背景样式不应再用于版本历史操作按钮
    assert "surface2" not in frag


# ---- 版本历史分页 ----

def test_version_history_page_size_constant():
    """VERSION_HISTORY_PAGE_SIZE 应为正整数."""
    from ila.dashboard import VERSION_HISTORY_PAGE_SIZE
    assert VERSION_HISTORY_PAGE_SIZE == 10
    assert isinstance(VERSION_HISTORY_PAGE_SIZE, int)
    assert VERSION_HISTORY_PAGE_SIZE > 0


def test_versions_endpoint_returns_paginated_structure(client, tmp_path):
    """GET /api/versions 返回的 JSON 应包含分页字段."""
    res = client.get("/api/versions")
    assert res.status_code == 200
    data = res.json()
    assert "versions" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "total_pages" in data
    assert data["page"] == 1
    assert data["total_pages"] >= 1


def test_versions_endpoint_page_param(client, monkeypatch, tmp_path):
    """GET /api/versions?page=N 应正确分页."""
    from ila.core.orchestrator import ILAOrchestrator
    from ila.core.registry import VersionRegistry

    def fake_discover(self, platform=None):
        return [{"object_id": f"test:skill:obj{i:03d}", "object_type": "skill",
                 "current_version": "v1"} for i in range(5)]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)
    client.post("/api/discover")

    # Seed 30 versions across 5 objects (6 each)
    reg = VersionRegistry(ila_home=str(tmp_path))
    for i in range(5):
        from ila.models.managed_object import ManagedObject
        oid = f"test:skill:obj{i:03d}"
        reg.register_object(ManagedObject(
            object_id=oid, platform="test", object_type="skill",
            name=f"obj{i:03d}", path="/tmp/dummy", current_version="v0",
        ))
        for v in range(6):
            vid = reg.create_version(oid, f"v{v}", sandbox_path="/tmp/sb")
            if v % 2 == 0:
                reg.update_version_status(vid, "live")

    res1 = client.get("/api/versions?page=1&page_size=10")
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["page"] == 1
    assert len(data1["versions"]) == 10
    assert data1["total_pages"] == 3

    res2 = client.get("/api/versions?page=2&page_size=10")
    data2 = res2.json()
    assert data2["page"] == 2
    assert len(data2["versions"]) == 10

    res3 = client.get("/api/versions?page=3&page_size=10")
    data3 = res3.json()
    assert data3["page"] == 3
    assert len(data3["versions"]) == 10


def test_versions_endpoint_custom_page_size(client, monkeypatch, tmp_path):
    """GET /api/versions?page_size=N 应使用自定义分页大小."""
    from ila.core.orchestrator import ILAOrchestrator
    from ila.core.registry import VersionRegistry

    def fake_discover(self, platform=None):
        return [{"object_id": "test:skill:objA", "object_type": "skill",
                 "current_version": "v1"}]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)
    client.post("/api/discover")

    reg = VersionRegistry(ila_home=str(tmp_path))
    from ila.models.managed_object import ManagedObject
    reg.register_object(ManagedObject(
        object_id="test:skill:objA", platform="test", object_type="skill",
        name="objA", path="/tmp/dummy", current_version="v0",
    ))
    for v in range(20):
        reg.create_version("test:skill:objA", f"v{v}", sandbox_path="/tmp/sb")

    res = client.get("/api/versions?page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert data["page_size"] == 5
    assert len(data["versions"]) == 5
    assert data["total_pages"] == 4


def test_versions_endpoint_page_out_of_range_clamped(client, monkeypatch, tmp_path):
    """超出范围的 page 参数应被钳制到有效范围."""
    from ila.core.orchestrator import ILAOrchestrator
    from ila.core.registry import VersionRegistry

    def fake_discover(self, platform=None):
        return [{"object_id": "test:skill:objA", "object_type": "skill",
                 "current_version": "v1"}]

    monkeypatch.setattr(ILAOrchestrator, "discover", fake_discover)
    client.post("/api/discover")

    reg = VersionRegistry(ila_home=str(tmp_path))
    from ila.models.managed_object import ManagedObject
    reg.register_object(ManagedObject(
        object_id="test:skill:objA", platform="test", object_type="skill",
        name="objA", path="/tmp/dummy", current_version="v0",
    ))
    for v in range(5):
        reg.create_version("test:skill:objA", f"v{v}", sandbox_path="/tmp/sb")

    # page=0 -> clamped to 1
    res = client.get("/api/versions?page=0&page_size=10")
    data = res.json()
    assert data["page"] == 1

    # page=999 -> clamped to total_pages
    res = client.get("/api/versions?page=999&page_size=10")
    data = res.json()
    assert data["page"] == 1


def test_versions_endpoint_empty(client):
    """无版本记录时仍返回合法分页结构."""
    res = client.get("/api/versions")
    assert res.status_code == 200
    data = res.json()
    assert data["versions"] == []
    assert data["total"] == 0
    assert data["total_pages"] == 1
    assert data["page"] == 1


def test_version_history_pagination_ui_present(client):
    """版本历史分页 UI 元素应存在于仪表板 HTML 中."""
    html = client.get("/").text
    assert 'id="versions-pagination"' in html
    assert "renderVersionPagination" in html
