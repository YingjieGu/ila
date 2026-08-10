"""OpenClaw 平台适配器."""

from __future__ import annotations

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

    对接 OpenClaw 的能力纳管模型，支持:
    - Skills: ~/.openclaw/skills/<name>/
    """

    def __init__(self, openclaw_home: str = "~/.openclaw"):
        self.openclaw_home = os.path.expanduser(openclaw_home)

    def platform_id(self) -> str:
        return "openclaw"

    def get_platform_home(self) -> str:
        return self.openclaw_home

    # ---- 对象发现 ----

    def discover_objects(self) -> list[ManagedObject]:
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
