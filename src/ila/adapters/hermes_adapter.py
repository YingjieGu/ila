"""Hermes Agent 平台适配器."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


class HermesAdapter(PlatformAdapter):
    """Hermes Agent 平台适配器.

    对接 Hermes Agent 的能力纳管模型，支持:
    - Skills: ~/.hermes/skills/<name>/SKILL.md
    - MCP Servers: config.yaml 中 mcp.* 配置
    - Plugins: ~/.hermes/plugins/<name>/
    - Profiles (Agent): ~/.hermes/profiles/<name>/
    """

    def __init__(self, hermes_home: str = "~/.hermes",
                 staging_profile: str = "ila-test"):
        self.hermes_home = os.path.expanduser(hermes_home)
        self.staging_profile = staging_profile

    def platform_id(self) -> str:
        return "hermes"

    def get_platform_home(self) -> str:
        return self.hermes_home

    # ---- 对象发现 ----

    def discover_objects(self) -> list[ManagedObject]:
        """扫描 Hermes 纳管的所有能力对象."""
        objects: list[ManagedObject] = []
        objects.extend(self._discover_skills())
        objects.extend(self._discover_mcp_servers())
        objects.extend(self._discover_plugins())
        objects.extend(self._discover_profiles())
        logger.info("Hermes: 发现 %d 个纳管对象", len(objects))
        return objects

    def _discover_skills(self) -> list[ManagedObject]:
        """发现所有 Skills."""
        objects = []
        skills_dir = os.path.join(self.hermes_home, "skills")
        if not os.path.isdir(skills_dir):
            return objects
        for entry in os.scandir(skills_dir):
            if not entry.is_dir():
                continue
            skill_md = os.path.join(entry.path, "SKILL.md")
            if os.path.exists(skill_md):
                version = self._read_skill_version(entry.path)
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("hermes", "skill", entry.name),
                    platform="hermes",
                    object_type="skill",
                    name=entry.name,
                    path=entry.path,
                    current_version=version,
                    metadata={"has_skill_md": True},
                ))
            else:
                # 可能有子目录 (categories)
                for sub in os.scandir(entry.path):
                    if sub.is_dir() and os.path.exists(os.path.join(sub.path, "SKILL.md")):
                        version = self._read_skill_version(sub.path)
                        objects.append(ManagedObject(
                            object_id=ManagedObject.make_id("hermes", "skill", sub.name),
                            platform="hermes",
                            object_type="skill",
                            name=sub.name,
                            path=sub.path,
                            current_version=version,
                            metadata={"category": entry.name},
                        ))
        return objects

    def _discover_mcp_servers(self) -> list[ManagedObject]:
        """发现 MCP Servers (从 config.yaml 读取)."""
        objects = []
        config_path = os.path.join(self.hermes_home, "config.yaml")
        if not os.path.exists(config_path):
            return objects
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f)
            mcp_servers = config.get("mcp", {}).get("servers", {})
            if isinstance(mcp_servers, dict):
                for name, cfg in mcp_servers.items():
                    objects.append(ManagedObject(
                        object_id=ManagedObject.make_id("hermes", "mcp", name),
                        platform="hermes",
                        object_type="mcp",
                        name=name,
                        path=cfg.get("command", ""),
                        current_version="unknown",
                        metadata=cfg,
                    ))
        except Exception as e:
            logger.warning("读取 MCP 配置失败: %s", e)
        return objects

    def _discover_plugins(self) -> list[ManagedObject]:
        """发现 Plugins."""
        objects = []
        plugins_dir = os.path.join(self.hermes_home, "plugins")
        if not os.path.isdir(plugins_dir):
            return objects
        for entry in os.scandir(plugins_dir):
            if entry.is_dir():
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("hermes", "plugin", entry.name),
                    platform="hermes",
                    object_type="plugin",
                    name=entry.name,
                    path=entry.path,
                    current_version="unknown",
                    metadata={},
                ))
        return objects

    def _discover_profiles(self) -> list[ManagedObject]:
        """发现 Agent Profiles."""
        objects = []
        profiles_dir = os.path.join(self.hermes_home, "profiles")
        if not os.path.isdir(profiles_dir):
            return objects
        for entry in os.scandir(profiles_dir):
            if entry.is_dir():
                objects.append(ManagedObject(
                    object_id=ManagedObject.make_id("hermes", "agent", entry.name),
                    platform="hermes",
                    object_type="agent",
                    name=entry.name,
                    path=entry.path,
                    current_version="unknown",
                    metadata={},
                ))
        return objects

    def _read_skill_version(self, skill_path: str) -> str:
        """从 SKILL.md 的 YAML frontmatter 读取版本号."""
        skill_md = os.path.join(skill_path, "SKILL.md")
        if not os.path.exists(skill_md):
            return "unknown"
        try:
            with open(skill_md) as f:
                content = f.read(2000)
            # 解析 YAML frontmatter
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
        """获取指定对象."""
        for obj in self.discover_objects():
            if obj.object_id == object_id:
                return obj
        return None

    # ---- 快照与恢复 ----

    def create_snapshot(self, obj: ManagedObject) -> str:
        """创建对象当前版本的 tar.gz 快照."""
        snapshot_dir = os.path.join(self.hermes_home, "ila", "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = os.path.join(
            snapshot_dir, f"{obj.object_type}-{obj.name}-{timestamp}.tar.gz"
        )
        if os.path.isdir(obj.path):
            with tarfile.open(snapshot_path, "w:gz") as tar:
                tar.add(obj.path, arcname=os.path.basename(obj.path))
        else:
            # 对于非目录对象 (如 MCP)，备份配置
            with tarfile.open(snapshot_path, "w:gz") as tar:
                # 添加一个 info 文件
                info_path = os.path.join(snapshot_dir, f"{obj.name}-info.json")
                with open(info_path, "w") as f:
                    json.dump(obj.to_dict(), f, indent=2)
                tar.add(info_path, arcname="object-info.json")
                os.unlink(info_path)
        logger.info("快照已创建: %s", snapshot_path)
        return snapshot_path

    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        """从快照恢复对象."""
        if not os.path.exists(snapshot_path):
            logger.error("快照不存在: %s", snapshot_path)
            return False
        try:
            # 先删除当前版本 (如果有)
            if os.path.exists(obj.path):
                shutil.rmtree(obj.path)
            # 从快照恢复
            with tarfile.open(snapshot_path, "r:gz") as tar:
                tar.extractall(path=os.path.dirname(obj.path))
            logger.info("已从快照恢复: %s", snapshot_path)
            return True
        except Exception as e:
            logger.error("恢复快照失败: %s", e)
            return False

    # ---- 热切换与重载 ----

    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """Hermes 热切换：快照 → 原子替换 → 重载 → 健康检查 → 自动回滚."""
        snapshot_path = self.create_snapshot(obj)
        target_path = obj.path
        backup_path = target_path + ".ila-backup"

        try:
            # write-to-temp + atomic rename
            temp_path = target_path + ".ila-swapping"
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path)
            shutil.copytree(sandbox_path, temp_path)

            # 旧版 → backup
            if os.path.exists(target_path):
                os.rename(target_path, backup_path)
            # 新版 → live
            os.rename(temp_path, target_path)

            # 触发重载
            self.reload(obj)

            # 健康检查
            if not self.health_check(obj):
                # 自动回滚
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                    target_path + ".ila-failed"
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
        """触发 Hermes 重载.

        对于 Skill/MCP，文件替换后 Hermes 在新 session 中自动加载新版本。
        这里做文件完整性验证而非调用 hermes chat（后者太慢且需要 LLM）。
        """
        try:
            if obj.object_type == "skill":
                # 验证 SKILL.md 存在且格式正确
                skill_md = os.path.join(obj.path, "SKILL.md")
                if not os.path.exists(skill_md):
                    logger.warning("重载验证失败: SKILL.md 不存在")
                    return False
                with open(skill_md) as f:
                    content = f.read(500)
                if not content.startswith("---"):
                    logger.warning("重载验证失败: SKILL.md 缺少 frontmatter")
                    return False
                logger.info("Skill 文件完整性验证通过 (新 session 自动加载)")
                return True
            elif obj.object_type == "mcp":
                # MCP 重载需要重启 server，文件级别验证
                return True
            elif obj.object_type == "agent":
                logger.info("Agent (profile) 变更需要新 session 生效")
                return True
            return True
        except Exception as e:
            logger.warning("重载验证失败: %s", e)
            return False

    def health_check(self, obj: ManagedObject) -> bool:
        """Hermes 健康检查 — 文件完整性验证.

        通过检查文件是否存在和格式是否正确来判断健康状态，
        避免调用 hermes chat (太慢，需要 LLM 响应)。
        """
        try:
            if obj.object_type == "skill":
                # 检查 SKILL.md 存在且 frontmatter 正确
                skill_md = os.path.join(obj.path, "SKILL.md")
                if not os.path.exists(skill_md):
                    return False
                with open(skill_md) as f:
                    content = f.read(500)
                if not content.startswith("---"):
                    return False
                end = content.find("---", 3)
                if end < 0:
                    return False
                # 检查 frontmatter 中有 name 字段
                frontmatter = content[3:end]
                if "name:" not in frontmatter:
                    return False
                return True
            elif obj.object_type == "mcp":
                result = subprocess.run(
                    ["hermes", "mcp", "test", obj.name],
                    capture_output=True, text=True, timeout=30,
                )
                return result.returncode == 0
            elif obj.object_type == "plugin":
                return os.path.isdir(obj.path)
            elif obj.object_type == "agent":
                return os.path.isdir(obj.path)
            return True
        except Exception as e:
            logger.warning("健康检查失败: %s", e)
            return False

    # ---- Staging 与调用 ----

    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str | dict:
        """通过 ila-test profile 创建 staging 环境.

        Returns:
            若对象含 HTML 文件则返回 dict {staging_id, staging_url, html_file}，
            否则返回 staging_id 字符串（向后兼容）。
        """
        staging_id = f"ila-staging-{obj.name}-{int(time.time())}"

        # 确保 staging profile 存在
        try:
            subprocess.run(
                ["hermes", "profile", "create", self.staging_profile, "--clone",
                 "--no-alias", "--description", "ILA staging profile"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass  # profile 可能已存在

        # 将沙箱新版本复制到 staging profile
        staging_path = None
        if obj.object_type == "skill":
            staging_path = os.path.join(
                self.hermes_home, "profiles", self.staging_profile, "skills", obj.name
            )
            if os.path.exists(staging_path):
                shutil.rmtree(staging_path)
            shutil.copytree(sandbox_path, staging_path)

        logger.info("Staging 部署完成: %s -> %s", obj.name, staging_id)

        # 检测 HTML 文件，生成 staging URL
        html_files = self._find_html_files(staging_path) if staging_path else []
        if html_files:
            main_html = html_files[0]  # 取第一个 HTML 作为主页面
            # staging URL 指向 Dashboard 的静态文件路由 (9527)
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

        对于 Skill，直接检查文件内容而非调用 hermes chat (太慢)。
        如果 test_input 包含 "check_file" 和 "expect_contains"，则做文件内容检查。
        否则返回文件存在性检查结果。
        """
        if obj.object_type == "skill":
            # 轻量级文件内容检查
            check_file = test_input.get("check_file", "")
            expect_contains = test_input.get("expect_contains", "")

            if check_file:
                file_path = os.path.join(obj.path, check_file)
                if not os.path.exists(file_path):
                    return {"output": "", "exit_code": 1, "error": f"文件不存在: {check_file}"}
                with open(file_path) as f:
                    content = f.read()
                if expect_contains and expect_contains not in content:
                    return {
                        "output": content[:200],
                        "exit_code": 0,
                        "error": f"未找到期望内容: {expect_contains}",
                    }
                return {"output": content[:200], "exit_code": 0, "error": ""}
            # 默认：检查 SKILL.md 存在
            skill_md = os.path.join(obj.path, "SKILL.md")
            exists = os.path.exists(skill_md)
            return {
                "output": "OK" if exists else "MISSING",
                "exit_code": 0 if exists else 1,
                "error": "",
            }
        return {"output": "", "exit_code": 0}

    def invoke_staging(self, staging_id: str, test_input: dict) -> dict[str, Any]:
        """调用 staging 环境对象 — 轻量级模式.

        直接检查 staging profile 目录中的文件内容。
        """
        skill_name = test_input.get("skill", "")
        check_file = test_input.get("check_file", "")
        expect_contains = test_input.get("expect_contains", "")

        # staging 文件路径
        staging_skill_path = os.path.join(
            self.hermes_home, "profiles", self.staging_profile, "skills", skill_name
        )

        if check_file:
            file_path = os.path.join(staging_skill_path, check_file)
            if not os.path.exists(file_path):
                return {"output": "", "exit_code": 1, "error": f"staging 文件不存在: {check_file}"}
            with open(file_path) as f:
                content = f.read()
            if expect_contains and expect_contains not in content:
                return {
                    "output": content[:200],
                    "exit_code": 0,
                    "error": f"staging 未找到期望内容: {expect_contains}",
                }
            return {"output": content[:200], "exit_code": 0, "error": ""}

        # 默认：检查 SKILL.md 存在
        skill_md = os.path.join(staging_skill_path, "SKILL.md")
        exists = os.path.exists(skill_md)
        return {
            "output": "OK" if exists else "MISSING",
            "exit_code": 0 if exists else 1,
            "error": "",
        }

    def cleanup_staging(self, staging_id: str) -> None:
        """清理 staging 环境."""
        # staging profile 保留供下次使用，不删除
        pass

    # ---- 文件与兼容性 ----

    def get_object_files(self, obj: ManagedObject) -> list[str]:
        """获取对象包含的所有文件列表."""
        if not os.path.isdir(obj.path):
            return []
        files = []
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

        # 检查 SKILL.md 格式
        if obj.object_type == "skill":
            new_skill_md = os.path.join(sandbox_path, "SKILL.md")
            if not os.path.exists(new_skill_md):
                issues.append("新版本缺少 SKILL.md 文件")
            else:
                # 检查 frontmatter 格式
                with open(new_skill_md) as f:
                    content = f.read(500)
                if not content.startswith("---"):
                    warnings.append("SKILL.md 缺少 YAML frontmatter")
                else:
                    end = content.find("---", 3)
                    if end < 0:
                        issues.append("SKILL.md frontmatter 未正确关闭")

        # 检查文件完整性
        old_files = set(os.path.basename(f) for f in self.get_object_files(obj))
        new_files = set()
        for root, _dirs, filenames in os.walk(sandbox_path):
            for fname in filenames:
                new_files.add(fname)

        removed = old_files - new_files
        if removed:
            warnings.append(f"新版本缺少文件: {removed}")

        compatible = len(issues) == 0
        return {"compatible": compatible, "issues": issues, "warnings": warnings}
