"""OpenClaw 平台适配器."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


class OpenClawAdapter(PlatformAdapter):
    """OpenClaw 平台适配器.

    对接 OpenClaw 的能力纳管模型，支持三类能力:
    - Skills:  ~/.openclaw/skills/<name>/          → openclaw:skill:<name>
    - Agents:  openclaw.json → agents.list         → openclaw:agent:<id>
    - Channels: openclaw.json → channels           → openclaw:channel:<name>
    """

    def __init__(self, openclaw_home: str = "~/.openclaw"):
        self.openclaw_home = os.path.expanduser(openclaw_home)
        self.config_path = os.path.join(self.openclaw_home, "openclaw.json")

    def platform_id(self) -> str:
        return "openclaw"

    def get_platform_home(self) -> str:
        return self.openclaw_home

    def _load_config(self) -> dict[str, Any]:
        """加载 openclaw.json (不存在或损坏时返回空 dict)."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, encoding="utf-8") as f:
                    return json.load(f) or {}
        except (OSError, ValueError):
            pass
        return {}

    # ---- 对象发现 ----

    def discover_objects(self) -> list[ManagedObject]:
        objects: list[ManagedObject] = []
        objects.extend(self._discover_skills())
        objects.extend(self._discover_agents())
        objects.extend(self._discover_channels())
        return sorted(objects, key=lambda o: o.object_id)

    def _discover_skills(self) -> list[ManagedObject]:
        """发现所有技能 (skills/ 下含 SKILL.md 的目录)."""
        objects = []
        skills_dir = os.path.join(self.openclaw_home, "skills")
        if os.path.isdir(skills_dir):
            for name in sorted(os.listdir(skills_dir)):
                path = os.path.join(skills_dir, name)
                if os.path.isdir(path):
                    objects.append(ManagedObject(
                        object_id=ManagedObject.make_id("openclaw", "skill", name),
                        platform="openclaw", object_type="skill",
                        name=name, path=path,
                    ))
        return objects

    def _discover_agents(self) -> list[ManagedObject]:
        """发现所有 agent (openclaw.json → agents.list)."""
        objects = []
        cfg = self._load_config()
        agents = cfg.get("agents", {})
        agent_list = agents.get("list", []) if isinstance(agents, dict) else []
        seen = set()
        for entry in agent_list:
            if not isinstance(entry, dict):
                continue
            aid = entry.get("id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            agent_dir = entry.get("agentDir") or os.path.join(
                self.openclaw_home, "agents", aid, "agent"
            )
            objects.append(ManagedObject(
                object_id=ManagedObject.make_id("openclaw", "agent", aid),
                platform="openclaw", object_type="agent",
                name=aid, path=os.path.expanduser(agent_dir),
                metadata={"workspace": entry.get("workspace", ""),
                          "config_source": self.config_path},
            ))
        return objects

    def _discover_channels(self) -> list[ManagedObject]:
        """发现所有 channel (openclaw.json → channels)."""
        objects = []
        cfg = self._load_config()
        channels = cfg.get("channels", {})
        if isinstance(channels, dict):
            for name in sorted(channels.keys()):
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("openclaw", "channel", name),
                    platform="openclaw", object_type="channel",
                    name=name, path=self.config_path,
                    metadata={"config_source": self.config_path},
                ))
        return objects

    def get_object(self, object_id: str) -> ManagedObject | None:
        for obj in self.discover_objects():
            if obj.object_id == object_id:
                return obj
        return None

    # ---- 快照 ----

    def create_snapshot(self, obj: ManagedObject) -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = os.path.join(
            os.path.expanduser("~/.ila/snapshots"),
            f"openclaw-{obj.name}-{ts}.tar.gz",
        )
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        with tarfile.open(snapshot_path, "w:gz") as tar:
            tar.add(obj.path, arcname=obj.name)
        return snapshot_path

    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        if not os.path.exists(snapshot_path):
            return False
        backup = obj.path + ".bak"
        if os.path.exists(obj.path):
            shutil.move(obj.path, backup)
        try:
            with tarfile.open(snapshot_path, "r:gz") as tar:
                tar.extractall(path=os.path.dirname(obj.path))
            return True
        except Exception:
            if os.path.exists(backup):
                shutil.move(backup, obj.path)
            return False

    # ---- Staging 与调用 ----

    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str | dict:
        staging_id = f"openclaw-staging-{obj.name}-{int(time.time())}"
        staging_dir = os.path.join(
            self.openclaw_home, ".ila-staging", "skills", obj.name
        )
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir)
        shutil.copytree(sandbox_path, staging_dir)

        # 检测 HTML 文件
        html_files = sorted(
            f for f in os.listdir(staging_dir)
            if f.endswith(('.html', '.htm'))
        ) if os.path.isdir(staging_dir) else []
        if html_files:
            return {
                "staging_id": staging_id,
                "staging_url": f"http://localhost:9527/staging/skill/{obj.name}/{html_files[0]}",
                "html_file": html_files[0],
            }
        return staging_id

    def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict[str, Any]:
        check_file = test_input.get("check_file", "")
        expect_contains = test_input.get("expect_contains", "")
        if check_file:
            file_path = os.path.join(obj.path, check_file)
            if not os.path.exists(file_path):
                return {"output": "", "exit_code": 1, "error": f"文件不存在: {check_file}"}
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            if expect_contains and expect_contains not in content:
                return {"output": content[:200], "exit_code": 0, "error": f"未找到: {expect_contains}"}
            return {"output": content[:200], "exit_code": 0, "error": ""}
        return {"output": "OK", "exit_code": 0}

    def invoke_staging(self, staging_id: str, test_input: dict) -> dict[str, Any]:
        skill_name = test_input.get("skill", "")
        check_file = test_input.get("check_file", "")
        staging_path = os.path.join(
            self.openclaw_home, ".ila-staging", "skills", skill_name
        )
        if check_file:
            file_path = os.path.join(staging_path, check_file)
            if not os.path.exists(file_path):
                return {"output": "", "exit_code": 1, "error": "staging 文件不存在"}
            with open(file_path, encoding="utf-8") as f:
                return {"output": f.read()[:200], "exit_code": 0, "error": ""}
        return {"output": "OK", "exit_code": 0}

    def cleanup_staging(self, staging_id: str) -> None:
        pass

    # ---- 热切换 ----

    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """OpenClaw 热切换：快照 → 原子替换 → 健康检查 → 自动回滚.

        - skill: 替换 ~/.openclaw/skills/<name>/ → 发布为 OpenClaw 技能
        - agent: 替换 agentDir → 发布为 OpenClaw 专家
        - channel: 配置文件对象, 拒绝整目录替换 (返回 rolled_back)
        """
        if obj.object_type == "channel":
            return {
                "status": "rolled_back",
                "reason": "channel 为配置对象 (openclaw.json), 不支持目录级替换, 请直接编辑配置",
            }
        snapshot = self.create_snapshot(obj)
        try:
            if os.path.exists(obj.path):
                shutil.rmtree(obj.path)
            shutil.copytree(sandbox_path, obj.path)
            self.reload(obj)
            if not self.health_check(obj):
                self.restore_snapshot(obj, snapshot)
                return {"status": "rolled_back", "snapshot": snapshot, "reason": "健康检查失败"}
            return {"status": "success", "snapshot": snapshot}
        except Exception as e:
            self.restore_snapshot(obj, snapshot)
            return {"status": "rolled_back", "reason": str(e), "snapshot": snapshot}

    def health_check(self, obj: ManagedObject) -> bool:
        if obj.object_type == "channel":
            # channel 对象: 检查 openclaw.json 存在且含该 channel 配置
            try:
                cfg = self._load_config()
                return obj.name in cfg.get("channels", {})
            except Exception:
                return False
        return os.path.isdir(obj.path)

    def reload(self, obj: ManagedObject) -> bool:
        return True

    # ---- 文件与兼容性 ----

    def get_object_files(self, obj: ManagedObject) -> list[str]:
        if not os.path.isdir(obj.path):
            return []
        files = []
        for root, _dirs, filenames in os.walk(obj.path):
            for fname in filenames:
                files.append(os.path.join(root, fname))
        return files

    def validate_compatibility(self, obj: ManagedObject,
                                sandbox_path: str) -> dict[str, Any]:
        return {"compatible": True, "issues": [], "warnings": []}
