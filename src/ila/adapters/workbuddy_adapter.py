"""WorkBuddy 平台适配器.

对接 WorkBuddy 的能力纳管模型，支持两类能力目录:
- Skills:   ``{workbuddy_home}/skills/<name>/``
            (SKILL.md + agent.py + pyproject.toml)
- Experts:  ``{workbuddy_home}/plugins/marketplaces/my-experts/plugins/<name>/``
            (agents/*.yaml 定义文件)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


class WorkBuddyAdapter(PlatformAdapter):
    """WorkBuddy 平台适配器.

    对接 WorkBuddy 的能力纳管模型，支持:
    - Skills: ~/.workbuddy/skills/<name>/SKILL.md
    - Experts: ~/.workbuddy/plugins/marketplaces/my-experts/plugins/<name>/agents/*.yaml
    """

    # WorkBuddy 固定的专家能力目录 (相对 workbuddy_home)
    _EXPERT_PLUGINS_REL = os.path.join("plugins", "marketplaces", "my-experts", "plugins")

    def __init__(self, workbuddy_home: str = "~/.workbuddy"):
        self.workbuddy_home = os.path.expanduser(workbuddy_home)
        # staging_id -> 部署路径映射，供 invoke_staging 定位 staging 环境
        self._staging_dirs: dict[str, str] = {}

    def platform_id(self) -> str:
        return "workbuddy"

    def get_platform_home(self) -> str:
        return self.workbuddy_home

    # ---- 对象发现 ----

    def discover_objects(self) -> list[ManagedObject]:
        """扫描 WorkBuddy 纳管的所有能力对象 (技能 + 专家)."""
        objects: list[ManagedObject] = []
        objects.extend(self._discover_skills())
        objects.extend(self._discover_experts())
        logger.info("WorkBuddy: 发现 %d 个纳管对象", len(objects))
        return sorted(objects, key=lambda o: o.object_id)

    def _discover_skills(self) -> list[ManagedObject]:
        """发现所有技能 (仅收录含 SKILL.md 的目录)."""
        objects: list[ManagedObject] = []
        skills_dir = os.path.join(self.workbuddy_home, "skills")
        if not os.path.isdir(skills_dir):
            return objects
        for entry in os.scandir(skills_dir):
            if not entry.is_dir():
                continue
            if os.path.exists(os.path.join(entry.path, "SKILL.md")):
                version = self._read_skill_version(entry.path)
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("workbuddy", "skill", entry.name),
                    platform="workbuddy",
                    object_type="skill",
                    name=entry.name,
                    path=entry.path,
                    current_version=version,
                    metadata={"has_skill_md": True},
                ))
        return objects

    def _discover_experts(self) -> list[ManagedObject]:
        """发现所有专家 (仅收录含 agents/*.yaml 的目录)."""
        objects: list[ManagedObject] = []
        experts_dir = os.path.join(self.workbuddy_home, self._EXPERT_PLUGINS_REL)
        if not os.path.isdir(experts_dir):
            return objects
        for entry in os.scandir(experts_dir):
            if not entry.is_dir():
                continue
            if self._find_agent_yaml(entry.path):
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("workbuddy", "expert", entry.name),
                    platform="workbuddy",
                    object_type="expert",
                    name=entry.name,
                    path=entry.path,
                    current_version="unknown",
                    metadata={"has_agents_yaml": True},
                ))
        return objects

    def _find_agent_yaml(self, expert_path: str) -> str | None:
        """返回专家目录中第一个 agents/*.yaml 文件路径，不存在则返回 None."""
        agents_dir = os.path.join(expert_path, "agents")
        if not os.path.isdir(agents_dir):
            return None
        for fname in sorted(os.listdir(agents_dir)):
            if fname.endswith((".yaml", ".yml")):
                fpath = os.path.join(agents_dir, fname)
                if os.path.isfile(fpath):
                    return fpath
        return None

    def _read_skill_version(self, skill_path: str) -> str:
        """从 SKILL.md 的 YAML frontmatter 读取版本号."""
        skill_md = os.path.join(skill_path, "SKILL.md")
        if not os.path.exists(skill_md):
            return "unknown"
        try:
            with open(skill_md, encoding="utf-8") as f:
                content = f.read(2000)
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    frontmatter = content[3:end]
                    match = re.search(r"^version:\s*(.+)$", frontmatter, re.MULTILINE)
                    if match:
                        return match.group(1).strip().strip("'\"")
        except Exception:
            pass
        return "unknown"

    def get_object(self, object_id: str) -> ManagedObject | None:
        """获取指定对象 (支持 skill / expert 两种类型)."""
        parts = object_id.split(":", 2)
        if len(parts) < 3 or parts[0] != "workbuddy":
            return None
        obj_type, name = parts[1], parts[2]

        if obj_type == "skill":
            path = os.path.join(self.workbuddy_home, "skills", name)
            if os.path.isfile(os.path.join(path, "SKILL.md")):
                return ManagedObject(
                    object_id=object_id,
                    platform="workbuddy",
                    object_type="skill",
                    name=name,
                    path=path,
                    current_version=self._read_skill_version(path),
                    metadata={"has_skill_md": True},
                )
        elif obj_type == "expert":
            path = os.path.join(self.workbuddy_home, self._EXPERT_PLUGINS_REL, name)
            if self._find_agent_yaml(path):
                return ManagedObject(
                    object_id=object_id,
                    platform="workbuddy",
                    object_type="expert",
                    name=name,
                    path=path,
                    current_version="unknown",
                    metadata={"has_agents_yaml": True},
                )
        return None

    # ---- 快照与恢复 ----

    def create_snapshot(self, obj: ManagedObject) -> str:
        """创建对象当前版本的 tar.gz 快照 (跳过 __pycache__/.git/*.pyc)."""
        snapshot_dir = os.path.join(self.workbuddy_home, ".ila", "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = os.path.join(
            snapshot_dir, f"{obj.object_type}-{obj.name}-{timestamp}.tar.gz"
        )
        if os.path.isdir(obj.path):
            with tarfile.open(snapshot_path, "w:gz") as tar:
                tar.add(obj.path, arcname=obj.name, filter=self._snapshot_filter)
        else:
            # 对于非目录对象，备份对象信息
            with tarfile.open(snapshot_path, "w:gz") as tar:
                info_path = os.path.join(snapshot_dir, f"{obj.name}-info.json")
                with open(info_path, "w", encoding="utf-8") as f:
                    json.dump(obj.to_dict(), f, indent=2, ensure_ascii=False)
                tar.add(info_path, arcname="object-info.json")
                os.unlink(info_path)
        logger.info("快照已创建: %s", snapshot_path)
        return snapshot_path

    @staticmethod
    def _snapshot_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """过滤快照中不需要的内容 (缓存/版本控制/字节码).

        Returns:
            过滤后的 TarInfo，返回 None 表示排除该成员。
        """
        parts = member.name.split("/")
        if any(part in ("__pycache__", ".git") for part in parts):
            return None
        if member.name.endswith(".pyc"):
            return None
        return member

    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        """从快照恢复对象."""
        if not os.path.exists(snapshot_path):
            logger.error("快照不存在: %s", snapshot_path)
            return False
        backup_path = obj.path + ".ila-backup"
        try:
            # 先移除当前版本 (如果有)，避免新旧文件混杂
            if os.path.exists(obj.path):
                shutil.move(obj.path, backup_path)
            # 从快照恢复
            with tarfile.open(snapshot_path, "r:gz") as tar:
                tar.extractall(path=os.path.dirname(obj.path))
            # 恢复成功，清理备份
            if os.path.exists(backup_path):
                shutil.rmtree(backup_path)
            logger.info("已从快照恢复: %s", snapshot_path)
            return True
        except Exception as e:
            logger.error("恢复快照失败: %s", e)
            # 恢复失败，回退到备份
            if os.path.exists(backup_path):
                shutil.move(backup_path, obj.path)
            return False

    # ---- Staging 与调用 ----

    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str | dict:
        """将沙箱中的新版本部署到 WorkBuddy staging 环境.

        Returns:
            若对象含 HTML 文件则返回 dict {staging_id, staging_url, html_file}，
            否则返回 staging_id 字符串（向后兼容）。
        """
        staging_id = f"workbuddy-staging-{obj.name}-{int(time.time())}"
        staging_dir = self._staging_path(obj.object_type, obj.name)
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir)
        os.makedirs(os.path.dirname(staging_dir), exist_ok=True)
        shutil.copytree(sandbox_path, staging_dir)
        self._staging_dirs[staging_id] = staging_dir

        logger.info("Staging 部署完成: %s -> %s", obj.name, staging_id)

        # 检测 HTML 文件，生成 staging URL
        html_files = self._find_html_files(staging_dir)
        if html_files:
            main_html = html_files[0]  # 取第一个 HTML 作为主页面
            staging_url = (
                f"http://localhost:9527/staging/{obj.object_type}/{obj.name}/{main_html}"
            )
            logger.info("检测到 HTML 文件，staging URL: %s", staging_url)
            return {
                "staging_id": staging_id,
                "staging_url": staging_url,
                "html_file": main_html,
            }

        return staging_id

    def _staging_path(self, object_type: str, name: str) -> str:
        """构造 staging 目标路径 (按对象类型区分)."""
        if object_type == "expert":
            return os.path.join(
                self.workbuddy_home, ".ila-staging", self._EXPERT_PLUGINS_REL, name
            )
        return os.path.join(self.workbuddy_home, ".ila-staging", "skills", name)

    @staticmethod
    def _find_html_files(directory: str) -> list[str]:
        """在目录中查找 HTML 文件（仅顶层，按名称排序）."""
        if not os.path.isdir(directory):
            return []
        html_files = sorted(
            f for f in os.listdir(directory)
            if f.endswith(('.html', '.htm')) and os.path.isfile(os.path.join(directory, f))
        )
        return html_files

    def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict[str, Any]:
        """调用线上对象 — 轻量级模式.

        支持:
        - ``check_file`` + ``expect_contains`` 文件内容检查
        - ``run_agent`` 参数: 若能力含 agent.py，用 subprocess 执行 ``python agent.py <args>``
          验证可运行性
        """
        # 运行 agent.py 验证可运行性
        if test_input.get("run_agent"):
            return self._run_agent_file(obj.path, test_input.get("run_agent"))

        check_file = test_input.get("check_file", "")
        expect_contains = test_input.get("expect_contains", "")

        if check_file:
            file_path = os.path.join(obj.path, check_file)
            if not os.path.exists(file_path):
                return {"output": "", "exit_code": 1, "error": f"文件不存在: {check_file}"}
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            if expect_contains and expect_contains not in content:
                return {
                    "output": content[:200],
                    "exit_code": 0,
                    "error": f"未找到期望内容: {expect_contains}",
                }
            return {"output": content[:200], "exit_code": 0, "error": ""}

        # 默认：检查核心能力文件 (skill: SKILL.md / expert: agents/*.yaml)
        core = self._core_file(obj)
        if core:
            exists = os.path.isfile(os.path.join(obj.path, core))
            return {
                "output": "OK" if exists else "MISSING",
                "exit_code": 0 if exists else 1,
                "error": "",
            }
        return {"output": "OK", "exit_code": 0}

    def invoke_staging(self, staging_id: str, test_input: dict) -> dict[str, Any]:
        """调用 staging 环境对象 — 轻量级模式.

        优先通过 deploy_to_staging 记录的 staging_id -> 路径映射定位；
        兼容旧调用方式（仅凭 test_input 中的 skill/expert 名称推导）。
        """
        staging_path = self._staging_dirs.get(staging_id, "")
        if not staging_path:
            # 兼容旧调用：从 test_input 推导路径
            name = test_input.get("skill") or test_input.get("expert") or ""
            obj_type = "expert" if test_input.get("expert") else "skill"
            staging_path = self._staging_path(obj_type, name)

        # 运行 agent.py 验证可运行性
        if test_input.get("run_agent"):
            return self._run_agent_file(staging_path, test_input.get("run_agent"))

        check_file = test_input.get("check_file", "")
        expect_contains = test_input.get("expect_contains", "")

        if check_file:
            file_path = os.path.join(staging_path, check_file)
            if not os.path.exists(file_path):
                return {"output": "", "exit_code": 1, "error": f"staging 文件不存在: {check_file}"}
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            if expect_contains and expect_contains not in content:
                return {
                    "output": content[:200],
                    "exit_code": 0,
                    "error": f"staging 未找到期望内容: {expect_contains}",
                }
            return {"output": content[:200], "exit_code": 0, "error": ""}

        # 默认：检查核心能力文件
        exists = False
        if os.path.isdir(staging_path):
            if os.path.isfile(os.path.join(staging_path, "SKILL.md")):
                exists = True
            elif self._find_agent_yaml(staging_path):
                exists = True
        return {
            "output": "OK" if exists else "MISSING",
            "exit_code": 0 if exists else 1,
            "error": "",
        }

    def _run_agent_file(self, base_path: str, run_agent: Any) -> dict[str, Any]:
        """执行 agent.py 验证能力可运行性.

        Args:
            base_path: 能力目录 (线上或 staging)
            run_agent: agent.py 参数，可为字符串、参数列表或 True(无参)

        Returns:
            ``{"output": str, "exit_code": int, "error": str}``
        """
        agent_script = os.path.join(base_path, "agent.py")
        if not os.path.exists(agent_script):
            return {"output": "", "exit_code": 1, "error": f"agent.py 不存在: {base_path}"}

        args: list[str] = []
        if isinstance(run_agent, str):
            args = shlex.split(run_agent)
        elif isinstance(run_agent, (list, tuple)):
            args = [str(a) for a in run_agent]

        cmd = [sys.executable, agent_script] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, encoding="utf-8"
            )
            return {
                "output": (result.stdout or "")[:500],
                "exit_code": result.returncode,
                "error": (result.stderr or "")[:500] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"output": "", "exit_code": 1, "error": "agent.py 执行超时"}
        except Exception as e:
            return {"output": "", "exit_code": 1, "error": f"agent.py 执行失败: {e}"}

    def cleanup_staging(self, staging_id: str) -> None:
        """清理 staging 记录 (保留文件供 Dashboard staging URL 引用)."""
        self._staging_dirs.pop(staging_id, None)

    # ---- 热切换与重载 ----

    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """WorkBuddy 热切换：快照 → 原子替换 → 健康检查 → 自动回滚.

        替换到真实能力目录 (skills 或 experts)，上线后 WorkBuddy 平台
        新 session 可直接发现新能力。
        """
        snapshot_path = self.create_snapshot(obj)
        target_path = obj.path
        backup_path = target_path + ".ila-backup"

        try:
            # write-to-temp + atomic rename
            temp_path = target_path + ".ila-swapping"
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path)
            shutil.copytree(sandbox_path, temp_path)

            # 旧版 → backup，新版 → live
            if os.path.exists(target_path):
                os.rename(target_path, backup_path)
            os.rename(temp_path, target_path)

            # 触发重载
            self.reload(obj)

            # 健康检查
            if not self.health_check(obj):
                # 自动回滚
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                if os.path.exists(backup_path):
                    os.rename(backup_path, target_path)
                self.reload(obj)
                return {
                    "status": "rolled_back",
                    "reason": "health check failed",
                    "snapshot": snapshot_path,
                }

            # 清理 backup
            if os.path.exists(backup_path):
                shutil.rmtree(backup_path)

            logger.info("热切换成功: %s", obj.object_id)
            return {"status": "success", "snapshot": snapshot_path}

        except Exception as e:
            # 紧急回滚
            logger.error("热切换异常: %s", e)
            if os.path.exists(backup_path) and not os.path.exists(target_path):
                os.rename(backup_path, target_path)
            return {"status": "error", "reason": str(e), "snapshot": snapshot_path}

    def reload(self, obj: ManagedObject) -> bool:
        """触发 WorkBuddy 重载.

        WorkBuddy 新 session 会自动加载文件变化，这里做文件完整性验证。
        """
        try:
            if obj.object_type == "skill":
                skill_md = os.path.join(obj.path, "SKILL.md")
                if not os.path.isfile(skill_md):
                    logger.warning("重载验证失败: SKILL.md 不存在")
                    return False
                with open(skill_md, encoding="utf-8") as f:
                    content = f.read(500)
                if not content.startswith("---"):
                    logger.warning("重载验证失败: SKILL.md 缺少 frontmatter")
                    return False
                logger.info("Skill 文件完整性验证通过 (新 session 自动加载)")
                return True
            elif obj.object_type == "expert":
                if self._find_agent_yaml(obj.path):
                    logger.info("Expert 文件完整性验证通过 (新 session 自动加载)")
                    return True
                logger.warning("重载验证失败: agents/*.yaml 不存在")
                return False
            return True
        except Exception as e:
            logger.warning("重载验证失败: %s", e)
            return False

    def health_check(self, obj: ManagedObject) -> bool:
        """WorkBuddy 健康检查 — 目录存在 + 核心文件存在.

        技能: SKILL.md；专家: agents/*.yaml 任一。
        """
        try:
            if not os.path.isdir(obj.path):
                return False
            core = self._core_file(obj)
            if core is None:
                return False
            return os.path.isfile(os.path.join(obj.path, core))
        except Exception as e:
            logger.warning("健康检查失败: %s", e)
            return False

    def _core_file(self, obj: ManagedObject) -> str | None:
        """返回对象的核心能力文件相对路径 (用于健康检查/调用验证).

        skill → ``SKILL.md``；expert → 第一个 ``agents/*.yaml``。
        """
        if obj.object_type == "skill":
            return "SKILL.md"
        if obj.object_type == "expert":
            agent_yaml = self._find_agent_yaml(obj.path)
            if agent_yaml:
                return os.path.relpath(agent_yaml, obj.path)
        return None

    # ---- 文件与兼容性 ----

    def get_object_files(self, obj: ManagedObject) -> list[str]:
        """获取对象包含的所有文件列表."""
        if not os.path.isdir(obj.path):
            return []
        files: list[str] = []
        for root, _dirs, filenames in os.walk(obj.path):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                files.append(fpath)
        return files

    def validate_compatibility(self, obj: ManagedObject,
                                sandbox_path: str) -> dict[str, Any]:
        """验证新版本与平台其他对象的兼容性."""
        issues: list[str] = []
        warnings: list[str] = []

        # 检查核心定义文件完整性
        if obj.object_type == "skill":
            new_skill_md = os.path.join(sandbox_path, "SKILL.md")
            if not os.path.exists(new_skill_md):
                issues.append("新版本缺少 SKILL.md 文件")
            else:
                with open(new_skill_md, encoding="utf-8") as f:
                    content = f.read(500)
                if not content.startswith("---"):
                    warnings.append("SKILL.md 缺少 YAML frontmatter")
                else:
                    end = content.find("---", 3)
                    if end < 0:
                        issues.append("SKILL.md frontmatter 未正确关闭")
        elif obj.object_type == "expert":
            new_agents = os.path.join(sandbox_path, "agents")
            if not os.path.isdir(new_agents):
                issues.append("新版本缺少 agents 目录")
            else:
                yaml_files = [
                    f for f in os.listdir(new_agents)
                    if f.endswith((".yaml", ".yml"))
                    and os.path.isfile(os.path.join(new_agents, f))
                ]
                if not yaml_files:
                    issues.append("新版本 agents 目录中缺少 *.yaml 定义文件")

        # 检查文件完整性
        old_files = set(os.path.basename(f) for f in self.get_object_files(obj))
        new_files: set[str] = set()
        for root, _dirs, filenames in os.walk(sandbox_path):
            for fname in filenames:
                new_files.add(fname)

        removed = old_files - new_files
        if removed:
            warnings.append(f"新版本缺少文件: {removed}")

        compatible = len(issues) == 0
        return {"compatible": compatible, "issues": issues, "warnings": warnings}
