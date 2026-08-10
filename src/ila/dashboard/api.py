"""ILA Dashboard 后端 - FastAPI REST API + 轮询推送."""

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
from ila import (
    VERSION_OPERATIONS,
    get_operation_label,
    get_operation_target_status,
    get_version_operations,
    is_valid_version_operation,
)
from ila.dashboard import (
    AVAILABLE_THEMES,
    DEFAULT_THEME,
    DEFAULT_PAGE_SIZE,
    VERSION_HISTORY_PAGE_SIZE,
    get_themes,
    is_valid_theme,
    resolve_theme,
)
from ila.adapters.ila_self_adapter import IlaSelfAdapter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 服务器启动时间戳 — 用于前端重连检测（区分新旧进程）
STARTUP_TIME = time.time()


# ---- 请求体模型 (必须在模块级别定义，FastAPI 才能正确识别为 Body 参数) ----

class RunRequest(BaseModel):
    object_id: str
    requirement: str
    auto_approve: bool = False

class RollbackRequest(BaseModel):
    object_id: str


class VersionOperateRequest(BaseModel):
    operation: str


# ---- 主题切换 ----
class ThemeRequest(BaseModel):
    theme: str


# ---- 线程安全的运行状态 (供前端轮询) ----

class RunState:
    """单次迭代的实时状态，后台线程写、HTTP 轮询读."""

    def __init__(self):
        self._lock = threading.Lock()
        self._phases: dict[str, dict] = {}      # phase_id -> {status, detail, ts}
        self._logs: list[dict] = []              # [{level, message, ts}]
        self._status: str = "idle"               # idle | running | success | error
        self._result: dict | None = None
        self._seq: int = 0                       # 单调递增序号，前端用它判断有无新数据
        self._context: dict[str, str] = {}       # 迭代上下文信息（沙箱路径、服务URL等）

    def reset(self):
        with self._lock:
            self._phases.clear()
            self._logs.clear()
            self._status = "running"
            self._result = None
            self._seq = 0
            self._context.clear()

    def set_context(self, updates: dict[str, str]):
        with self._lock:
            self._context.update(updates)
            self._seq += 1

    def set_phase(self, phase: str, status: str, detail: str = ""):
        with self._lock:
            self._phases[phase] = {"status": status, "detail": detail, "ts": time.time()}
            self._seq += 1

    def add_log(self, level: str, message: str):
        with self._lock:
            self._logs.append({"level": level, "message": message, "ts": time.time()})
            # 保留最近 200 条
            if len(self._logs) > 200:
                self._logs = self._logs[-200:]
            self._seq += 1

    def complete(self, status: str, result: dict):
        with self._lock:
            self._status = status
            self._result = result
            self._seq += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "phases": dict(self._phases),
                "logs": list(self._logs),
                "result": self._result,
                "seq": self._seq,
                "context": dict(self._context),
            }

    @property
    def is_running(self) -> bool:
        return self._status == "running"


# 全局唯一运行状态 (同一时间只允许一个迭代)
_run_state = RunState()


def create_app(config: dict[str, Any], sandbox_manager: Any = None, port: int = 9527):
    """创建 FastAPI 应用."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="ILA Dashboard", version="1.0.0")

    # 初始化
    ila_home = os.path.expanduser(config.get("ila", {}).get("home", "~/.ila"))
    registry = VersionRegistry(ila_home=ila_home)
    orchestrator = ILAOrchestrator(config, sandbox_manager=sandbox_manager)
    app.state.registry = registry
    app.state.orchestrator = orchestrator
    app.state.port = port  # 当前端口 (9527=生产, 9528=staging)

    # Staging profile 名称 (用于静态文件服务)
    hermes_cfg = config.get("adapters", {}).get("hermes", {})
    hermes_home = os.path.expanduser(hermes_cfg.get("hermes_home", "~/.hermes"))
    staging_profile = hermes_cfg.get("staging_profile", "ila-test")
    app.state.hermes_home = hermes_home
    app.state.staging_profile = staging_profile

    # ---- 主题切换 (暗色 / 亮色 / 其他风格) ----
    dashboard_cfg = config.get("dashboard", {})
    current_theme = {"value": resolve_theme(dashboard_cfg.get("theme"))}

    @app.get("/api/themes")
    async def list_themes():
        """获取可用主题列表及当前主题."""
        return {
            "themes": get_themes(),
            "current": current_theme["value"],
            "default": DEFAULT_THEME,
        }

    @app.post("/api/theme")
    async def set_theme(req: ThemeRequest):
        """切换当前主题 (不支持的主题返回错误)."""
        if not is_valid_theme(req.theme):
            return {
                "status": "error",
                "reason": "不支持的主题",
                "supported": list(AVAILABLE_THEMES),
            }
        current_theme["value"] = req.theme
        return {"status": "ok", "current": req.theme}

    # ---- REST API ----

    @app.get("/api/status")
    async def get_status():
        """获取 ILA 全局状态."""
        stats = registry.get_stats()
        platforms = AdapterRegistry.get_registered_platforms()
        # 验证模式仅在 staging 端口 (9528) 激活;
        # 生产端口 (9527) 始终关闭, 确保上线后恢复正常状态
        verif = IlaSelfAdapter._load_verification_mode_static()
        is_staging = app.state.port == 9528
        verification_mode = (verif.get("enabled", True) if verif else True) and is_staging
        modified_modules = (verif.get("modified_modules", []) if verif else []) if is_staging else []
        return {
            **stats,
            "platforms_registered": platforms,
            "verification_mode": verification_mode,
            "modified_modules": modified_modules,
            "startup_time": STARTUP_TIME,
            "timestamp": time.time(),
        }

    @app.get("/api/objects")
    async def get_objects(platform: str | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
        """获取所有纳管对象 (支持分页).

        对象的 version 会根据迭代最新的版本号进行更新。
        """
        objects = orchestrator.discover(platform=platform)
        # 用注册表中最新迭代版本号同步各对象的 current_version
        for obj in objects:
            latest = registry.get_latest_version(obj["object_id"])
            if latest and latest.get("version"):
                obj["current_version"] = latest["version"]
        total = len(objects)
        total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size
        paged_objects = objects[start:end]
        return {
            "objects": paged_objects,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

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
    async def get_all_versions(page: int = 1, page_size: int = VERSION_HISTORY_PAGE_SIZE):
        """获取所有版本记录 (分页)."""
        page = max(1, page)
        page_size = max(1, page_size)
        objects = registry.get_all_objects()
        all_versions = []
        for obj in objects:
            versions = registry.get_versions_by_object(obj["object_id"])
            all_versions.extend(versions)
        all_versions.sort(key=lambda v: v.get("created_at", ""), reverse=True)
        total = len(all_versions)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = start + page_size
        paged_versions = all_versions[start:end]
        return {
            "versions": paged_versions,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    @app.post("/api/discover")
    async def trigger_discover(platform: str | None = None):
        """触发对象发现并注册."""
        objects = orchestrator.discover(platform=platform)
        return {"discovered": len(objects), "objects": objects}

    # ---- 迭代闭环 ----

    phase_names = {
        "analyze": "需求分析", "develop": "沙箱开发", "test": "A/B 对比测试",
        "verify": "部署验证 (AB 模式: 9528 启动新版本)",
        "switch": "正式上线 (9527 迁移)",
    }

    def _start_iteration(object_id: str, requirement: str, auto_approve: bool = False) -> dict:
        """在后台启动一次迭代闭环 (复用 _run_state 进度推送)."""
        if _run_state.is_running:
            return {"status": "busy", "message": "已有迭代正在执行，请等待完成"}

        _run_state.reset()

        def _on_progress(phase: str, status: str, detail: str | None,
                          context: dict | None = None):
            name = phase_names.get(phase, phase)
            detail_text = detail or ""
            _run_state.set_phase(phase, status, detail_text)

            if context:
                _run_state.set_context(context)
                ctx_parts = [f"{k}: {v}" for k, v in context.items() if v]
                if ctx_parts:
                    _run_state.add_log("info", "上下文信息: " + " | ".join(ctx_parts))

            if status == "running":
                _run_state.add_log("info", f"正在执行: {name}")
            elif status == "done":
                _run_state.add_log("success", f"{name} 完成{': ' + detail_text if detail_text else ''}")
            elif status == "failed":
                _run_state.add_log("error", f"{name} 失败{': ' + detail_text if detail_text else ''}")
            elif status == "skipped":
                _run_state.add_log("warn", f"{name} 跳过{': ' + detail_text if detail_text else ''}")

        def _run():
            try:
                result = orchestrator.run_iteration(
                    object_id, requirement,
                    auto_approve=auto_approve,
                    progress_callback=_on_progress,
                )
                if result["status"] == "pending_approval":
                    _run_state.add_log("warn", "热切换待用户确认，请在面板上点击「批准」或「跳过」")
                    _run_state.complete("pending_approval", result)
                else:
                    level = "success" if result["status"] == "success" else "error"
                    _run_state.add_log(level, f"迭代闭环结束: {result['status']}")
                    _run_state.complete(result["status"], result)
            except Exception as e:
                logger.exception("迭代闭环异常")
                _run_state.add_log("error", f"迭代闭环异常: {e}")
                _run_state.complete("error", {"status": "error", "reason": str(e)})

        t = threading.Thread(target=_run, daemon=False)
        t.start()

        return {
            "status": "started",
            "object_id": object_id,
            "requirement": requirement,
            "message": "迭代闭环已启动",
        }

    @app.post("/api/run")
    async def trigger_run(req: RunRequest):
        """触发迭代闭环 (后台执行，通过 /api/run/status 轮询进度)."""
        return _start_iteration(req.object_id, req.requirement, req.auto_approve)

    @app.get("/api/run/status")
    async def get_run_status():
        """轮询获取当前迭代的实时进度."""
        return _run_state.snapshot()

    @app.post("/api/run/approve")
    async def approve_run():
        """批准热切换 (当迭代处于 pending_approval 状态时)."""
        status = _run_state.snapshot()
        if status["status"] != "pending_approval":
            return {"status": "error", "reason": "当前没有待批准的热切换"}

        _run_state.reset()

        def _on_progress(phase: str, s: str, detail: str | None):
            phase_names = {
                "analyze": "需求分析",
                "plan": "生成计划",
                "code": "代码生成",
                "test": "A/B 测试",
                "deploy": "部署验证 (AB 模式)",
                "switch": "正式上线 (9527 迁移)",
            }
            name = phase_names.get(phase, phase)
            _run_state.set_phase(phase, s, detail or "")
            if s == "running":
                _run_state.add_log("info", f"正在执行: {name}")
            elif s == "done":
                _run_state.add_log("success", f"{name} 完成{': ' + (detail or '') if detail else ''}")
            elif s == "failed":
                _run_state.add_log("error", f"{name} 失败{': ' + (detail or '') if detail else ''}")

        # 从 result 中获取 target_object_id
        result_data = status.get("result", {})
        target_id = result_data.get("task_spec", {}).get("target_object_id", "")

        def _run_approve():
            try:
                result = orchestrator.approve_switch(
                    target_id,
                    progress_callback=_on_progress,
                )
                level = "success" if result["status"] == "success" else "error"
                _run_state.add_log(level, f"迭代闭环结束: {result['status']}")
                _run_state.complete(result["status"], result)
            except Exception as e:
                logger.exception("批准热切换异常")
                _run_state.add_log("error", f"批准热切换异常: {e}")
                _run_state.complete("error", {"status": "error", "reason": str(e)})

        t = threading.Thread(target=_run_approve, daemon=False)
        t.start()

        return {"status": "approved", "message": "正式上线已批准，正在执行迁移..."}

    @app.post("/api/run/skip")
    async def skip_run():
        """跳过热切换."""
        status = _run_state.snapshot()
        if status["status"] != "pending_approval":
            return {"status": "error", "reason": "当前没有待批准的热切换"}

        result = orchestrator.skip_switch()
        _run_state.add_log("warn", f"用户跳过热切换: {result.get('reason', '')}")
        _run_state.complete("skipped", result)
        return {"status": "skipped", "message": "已跳过热切换"}

    @app.post("/api/rollback")
    async def trigger_rollback(req: RollbackRequest):
        """触发回滚."""
        result = orchestrator.rollback(req.object_id)
        return result

    # ---- 版本历史操作 (按版本状态映射可用操作) ----

    @app.get("/api/version/operations")
    async def list_version_operations():
        """获取按版本状态映射的可用操作."""
        operations = {
            status: [
                {
                    "operation": op,
                    "label": get_operation_label(op),
                    "target_status": get_operation_target_status(op),
                }
                for op in ops
            ]
            for status, ops in VERSION_OPERATIONS.items()
        }
        return {"operations": operations}

    @app.post("/api/version/{version_id}/operate")
    async def operate_version(version_id: int, req: VersionOperateRequest):
        """对指定版本执行操作 (回滚/部署验证/停止/重新迭代)."""
        operation = req.operation
        if not is_valid_version_operation(operation):
            return {
                "status": "error",
                "reason": f"不支持的操作: {operation}",
            }

        version = registry.get_version(version_id)
        if not version:
            return {"status": "error", "reason": f"版本不存在: {version_id}"}

        status = version.get("status", "")
        allowed = get_version_operations(status)
        if operation not in allowed:
            return {
                "status": "error",
                "reason": f"版本状态 '{status}' 不支持操作 '{operation}'",
                "allowed": allowed,
            }

        object_id = version.get("object_id", "")

        if operation == "rollback":
            return orchestrator.rollback(object_id)

        if operation == "deploy_verify":
            registry.update_version_status(
                version_id,
                "verified",
                deploy_verification={"verified_at": time.time(), "by": "dashboard"},
            )
            return {
                "status": "success",
                "version_id": version_id,
                "operation": operation,
                "version_status": get_operation_target_status(operation),
            }

        if operation == "stop":
            registry.update_version_status(version_id, "stopped")
            return {
                "status": "success",
                "version_id": version_id,
                "operation": operation,
                "version_status": get_operation_target_status(operation),
            }

        # operation == "iterate": 重新迭代 - 回到 developing 并后台触发新一轮闭环
        registry.update_version_status(version_id, "developing")
        task_spec = version.get("task_spec") or {}
        requirement = task_spec.get("requirement", "") if isinstance(task_spec, dict) else ""
        run_result = None
        if requirement and object_id:
            try:
                run_result = _start_iteration(object_id, requirement, auto_approve=False)
            except Exception as e:
                logger.exception("重新迭代触发异常")
                run_result = {"status": "error", "reason": str(e)}
        return {
            "status": "success",
            "version_id": version_id,
            "operation": operation,
            "version_status": get_operation_target_status(operation),
            "run": run_result,
        }

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
                with open(path, encoding="utf-8") as f:
                    result[ext] = f.read()
        return result if result else {"error": "报告不存在"}

    @app.get("/api/reports")
    async def list_reports(page: int = 1, page_size: int = 10):
        """列出所有报告 (支持分页)."""
        report_dir = os.path.expanduser(
            config.get("report", {}).get("output_dir", "~/.ila/reports")
        )
        reports = []
        if os.path.isdir(report_dir):
            for fname in sorted(os.listdir(report_dir), reverse=True):
                if fname.endswith(".json"):
                    task_id = fname.rsplit(".", 1)[0]
                    path = os.path.join(report_dir, fname)
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    obj = data.get("object", {})
                    conclusion = data.get("conclusion", {})
                    reports.append({
                        "task_id": task_id,
                        "target": obj.get("object_id", ""),
                        "platform": obj.get("platform", ""),
                        "verdict": data.get("verdict", ""),
                        "verdict_label": conclusion.get("verdict_label", ""),
                        "verdict_icon": conclusion.get("verdict_icon", ""),
                        "timestamp": data.get("generated_at", ""),
                        "new_version": data.get("new_version", ""),
                        "total_cases": data.get("total_cases", 0),
                        "passed_cases": data.get("passed_cases", 0),
                        "failed_cases": data.get("failed_cases", 0),
                        "overall_text": conclusion.get("overall_text", ""),
                        "summary": conclusion.get("summary", ""),
                    })
        total = len(reports)
        total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        return {
            "reports": reports[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    # ---- WebSocket (保留兼容，不再主用) ----

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass

    # ---- 前端页面 ----

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """返回管控面板 HTML."""
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        if os.path.exists(html_path):
            with open(html_path, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>ILA Dashboard</h1><p>dashboard.html not found</p>")

    # ---- Staging 静态文件服务 ----
    # 部署验证时，staging URL 指向此路由以展示纳管对象的实际页面
    # 例: /staging/skill/minesweeper/minesweeper.html

    @app.get("/staging/{object_type:str}/{object_name:str}/{file_path:path}")
    async def serve_staging_file(object_type: str, object_name: str, file_path: str):
        """从 staging profile 目录提供纳管对象的静态文件."""
        from fastapi.responses import FileResponse, PlainTextResponse
        import mimetypes

        staging_dir = os.path.join(
            app.state.hermes_home, "profiles", app.state.staging_profile
        )

        # 构建 staging 文件路径: {staging_dir}/{object_type}s/{name}/{path}
        staging_path = os.path.join(staging_dir, f"{object_type}s", object_name, file_path)

        # 安全检查：防止目录穿越
        real_staging = os.path.realpath(staging_dir)
        real_path = os.path.realpath(staging_path)
        if not real_path.startswith(real_staging + os.sep) and real_path != real_staging:
            return PlainTextResponse("Forbidden", status_code=403)

        if not os.path.isfile(staging_path):
            return PlainTextResponse(f"File not found: {file_path}", status_code=404)

        mime_type, _ = mimetypes.guess_type(staging_path)
        return FileResponse(staging_path, media_type=mime_type or "application/octet-stream")

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
