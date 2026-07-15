"""ILA Dashboard 后端 — FastAPI REST API + WebSocket 实时推送."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from ila.adapters.registry import AdapterRegistry
from ila.core.orchestrator import ILAOrchestrator
from ila.core.registry import VersionRegistry

logger = logging.getLogger(__name__)


def create_app(config: dict[str, Any], sandbox_manager: Any = None):
    """创建 FastAPI 应用."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(title="ILA Dashboard", version="1.0.0")

    # 初始化
    ila_home = os.path.expanduser(config.get("ila", {}).get("home", "~/.ila"))
    registry = VersionRegistry(ila_home=ila_home)
    orchestrator = ILAOrchestrator(config, sandbox_manager=sandbox_manager)

    # WebSocket 连接管理
    active_connections: list[WebSocket] = []

    async def broadcast(message: dict):
        """广播消息到所有 WebSocket 连接."""
        for ws in active_connections:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    # ---- 数据模型 ----

    class RunRequest(BaseModel):
        object_id: str
        requirement: str
        auto_approve: bool = False

    class RollbackRequest(BaseModel):
        object_id: str

    # ---- REST API ----

    @app.get("/api/status")
    async def get_status():
        """获取 ILA 全局状态."""
        stats = registry.get_stats()
        platforms = AdapterRegistry.get_registered_platforms()
        return {
            **stats,
            "platforms_registered": platforms,
            "timestamp": time.time(),
        }

    @app.get("/api/objects")
    async def get_objects(platform: str | None = None):
        """获取所有纳管对象."""
        objects = orchestrator.discover(platform=platform)
        return {"objects": objects, "total": len(objects)}

    @app.get("/api/objects/{object_id:path}")
    async def get_object_detail(object_id: str):
        """获取对象详情 + 版本历史."""
        obj_data = registry.get_object(object_id)
        if not obj_data:
            return {"error": "对象不存在", "object_id": object_id}
        versions = registry.get_versions_by_object(object_id)
        test_cases = registry.get_test_cases(object_id)
        return {
            "object": obj_data,
            "versions": versions,
            "test_cases": test_cases,
        }

    @app.get("/api/versions")
    async def get_all_versions():
        """获取所有版本记录."""
        objects = registry.get_all_objects()
        all_versions = []
        for obj in objects:
            versions = registry.get_versions_by_object(obj["object_id"])
            all_versions.extend(versions)
        # 按创建时间倒序
        all_versions.sort(key=lambda v: v.get("created_at", ""), reverse=True)
        return {"versions": all_versions, "total": len(all_versions)}

    @app.post("/api/discover")
    async def trigger_discover(platform: str | None = None):
        """触发对象发现并注册."""
        objects = orchestrator.discover(platform=platform)
        return {"discovered": len(objects), "objects": objects}

    @app.post("/api/run")
    async def trigger_run(req: RunRequest):
        """触发迭代闭环 (后台执行)."""
        def _run():
            result = orchestrator.run_iteration(
                req.object_id, req.requirement, auto_approve=req.auto_approve
            )
            # 结果通过 WebSocket 推送
            # 在线程中不能直接 await，用线程安全方式
            pass

        # 在后台线程执行
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        return {
            "status": "started",
            "object_id": req.object_id,
            "requirement": req.requirement,
            "message": "迭代闭环已启动，请通过流程图查看进度",
        }

    @app.post("/api/rollback")
    async def trigger_rollback(req: RollbackRequest):
        """触发回滚."""
        result = orchestrator.rollback(req.object_id)
        return result

    @app.get("/api/reports/{task_id}")
    async def get_report(task_id: str):
        """获取报告内容."""
        report_dir = os.path.expanduser(
            config.get("report", {}).get("output_dir", "~/.ila/reports")
        )
        result = {}
        for ext in ("json", "md", "html"):
            path = os.path.join(report_dir, f"{task_id}.{ext}")
            if os.path.exists(path):
                with open(path) as f:
                    result[ext] = f.read()
        return result if result else {"error": "报告不存在"}

    @app.get("/api/reports")
    async def list_reports():
        """列出所有报告."""
        report_dir = os.path.expanduser(
            config.get("report", {}).get("output_dir", "~/.ila/reports")
        )
        reports = []
        if os.path.isdir(report_dir):
            for fname in sorted(os.listdir(report_dir), reverse=True):
                if fname.endswith(".json"):
                    task_id = fname.rsplit(".", 1)[0]
                    path = os.path.join(report_dir, fname)
                    with open(path) as f:
                        data = json.load(f)
                    reports.append({
                        "task_id": task_id,
                        "target": data.get("target", {}).get("object_id", ""),
                        "verdict": data.get("verdict", ""),
                        "timestamp": data.get("timestamp", ""),
                    })
        return {"reports": reports}

    # ---- WebSocket ----

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        active_connections.append(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                # 处理客户端消息 (如心跳)
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            active_connections.remove(websocket)

    # ---- 前端页面 ----

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """返回管控面板 HTML."""
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        if os.path.exists(html_path):
            with open(html_path) as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>ILA Dashboard</h1><p>dashboard.html not found</p>")

    @app.get("/api/flow/pipeline")
    async def get_pipeline_phases():
        """获取六阶段流程定义."""
        return {
            "phases": [
                {
                    "id": "analyze",
                    "name": "需求分析",
                    "icon": "🔍",
                    "description": "解析需求，识别目标对象，生成任务规格书",
                    "outputs": ["task_spec.json"],
                },
                {
                    "id": "develop",
                    "name": "沙箱开发",
                    "icon": "🔨",
                    "description": "创建隔离沙箱，调用 Codex CLI 开发新版本",
                    "outputs": ["sandbox 代码", "变更文件清单"],
                },
                {
                    "id": "test",
                    "name": "A/B 对比测试",
                    "icon": "🧪",
                    "description": "新旧版本对比测试，自动判定 pass/fail",
                    "outputs": ["test_report.json", "对比结果"],
                },
                {
                    "id": "verify",
                    "name": "部署验证",
                    "icon": "✅",
                    "description": "兼容性检查，确保不影响其他服务",
                    "outputs": ["验证结果"],
                },
                {
                    "id": "switch",
                    "name": "热切换上线",
                    "icon": "🚀",
                    "description": "原子替换文件 + 完整性验证",
                    "outputs": ["上线结果", "回滚快照"],
                },
                {
                    "id": "rollback",
                    "name": "回滚兜底",
                    "icon": "🔙",
                    "description": "健康检查失败时自动从快照恢复",
                    "outputs": ["回滚结果"],
                },
            ]
        }

    return app
