"""ILA 自升级适配器 — 将 ILA 自身作为纳管对象进行闭环迭代.

核心设计:
  - 平台 ID: "ila"
  - 对象 ID: "ila:agent:core"
  - 路径: ~/myprojects/ila/src/ila/

与其他适配器的关键区别:
  1. ILA 是运行中的进程，升级需要进程管理 (端口 9527 → 9528)
  2. A/B 测试通过 HTTP API 对比，而非文件内容对比
  3. 热切换涉及流量切换 (iptables/socat) 而非仅文件替换
  4. 旧进程保持运行作为回滚保底
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from typing import Any

from ila.adapters.base import PlatformAdapter
from ila.launcher_platform import health_check, wait_port_free
from ila.models.managed_object import ManagedObject

logger = logging.getLogger(__name__)


# Staging 信息存储路径
_STAGING_DIR = os.path.expanduser("~/.ila/staging")
# ILA 项目根目录
_ILA_PROJECT = os.path.expanduser("~/myprojects/ila")
# ILA 源码目录
_ILA_SRC = os.path.join(_ILA_PROJECT, "src", "ila")
# ILA 配置目录
_ILA_CONFIG = os.path.join(_ILA_PROJECT, "config")

# 验证模式文件路径 (staging 阶段标记修改的模块)
_VERIFICATION_MODE_FILE = os.path.expanduser("~/.ila/verification-mode.json")

# 受保护文件列表 (沙箱复制和部署时排除)
_PROTECTED_PATTERNS = [
    "__pycache__", ".git", "venv", "node_modules",
    ".pytest_cache", ".egg-info", ".mypy_cache",
    "registry.db", "ila_config.yaml",
    "logs", "reports", "snapshots", "staging",
]

# 默认端口
_DEFAULT_PORT = 9527
_STAGING_PORT = 9528


class IlaSelfAdapter(PlatformAdapter):
    """ILA 自升级适配器.

    将 ILA 自身 (src/ila/) 注册为 ``ila:agent:core`` 纳管对象，
    支持通过标准 ILA 闭环流程进行自升级、A/B 对比测试和热切换。
    """

    def __init__(self, project_root: str | None = None,
                 dashboard_port: int = _DEFAULT_PORT,
                 staging_port: int = _STAGING_PORT):
        self.project_root = os.path.expanduser(project_root or _ILA_PROJECT)
        self.src_dir = os.path.join(self.project_root, "src", "ila")
        self.config_dir = os.path.join(self.project_root, "config")
        self.dashboard_port = dashboard_port
        self.staging_port = staging_port
        os.makedirs(_STAGING_DIR, exist_ok=True)

    # ================================================================
    # 平台标识
    # ================================================================

    def platform_id(self) -> str:
        return "ila"

    def get_platform_home(self) -> str:
        return self.project_root

    # ================================================================
    # 对象发现
    # ================================================================

    def discover_objects(self) -> list[ManagedObject]:
        """发现 ILA 自身作为纳管对象."""
        version = self._read_version()
        return [
            ManagedObject(
                object_id="ila:agent:core",
                platform="ila",
                object_type="agent",
                name="ila-core",
                path=self.src_dir,
                current_version=version,
                metadata={
                    "project_root": self.project_root,
                    "dashboard_port": self.dashboard_port,
                    "staging_port": self.staging_port,
                    "protected_patterns": _PROTECTED_PATTERNS,
                    "description": "ILA Agent 自身 — 通过 ILA 闭环迭代自升级",
                },
            )
        ]

    def get_object(self, object_id: str) -> ManagedObject | None:
        """获取指定对象."""
        for obj in self.discover_objects():
            if obj.object_id == object_id:
                return obj
        return None

    def _read_version(self) -> str:
        """从 __init__.py 或 VERSION 文件读取版本号."""
        # 尝试读取 VERSION 文件
        version_file = os.path.join(self.src_dir, "VERSION")
        if os.path.exists(version_file):
            try:
                with open(version_file) as f:
                    return f.read().strip()
            except Exception:
                pass

        # 尝试从 __init__.py 读取 __version__
        init_file = os.path.join(self.src_dir, "__init__.py")
        if os.path.exists(init_file):
            try:
                with open(init_file) as f:
                    content = f.read(2000)
                match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        # 尝试从 pyproject.toml 读取
        pyproject = os.path.join(self.project_root, "pyproject.toml")
        if os.path.exists(pyproject):
            try:
                with open(pyproject) as f:
                    content = f.read(2000)
                match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        return "v1.0.0-from-src"

    # ================================================================
    # 验证模式 (deployment verification 阶段标识修改的模块)
    # ================================================================

    _MODULE_DIR_MAP = {
        "dashboard": {"name": "Dashboard UI", "has_visual": True},
        "core": {"name": "Core Engine", "has_visual": False},
        "adapters": {"name": "Adapters", "has_visual": False},
        "models": {"name": "Data Models", "has_visual": False},
        "cli": {"name": "CLI Interface", "has_visual": False},
    }

    def _detect_modified_modules(self, sandbox_path: str,
                                  baseline_path: str | None = None) -> list[dict]:
        """检测 sandbox 中被修改的文件（文件级颗粒度）.

        遍历沙箱所有文件与基准对比，每个改动文件独立标识。
        跳过 __pycache__、.pyc、AGENTS.md、.ila-sandbox.json。
        """
        if baseline_path is None:
            baseline_path = self.src_dir

        sandbox_src = os.path.join(sandbox_path, "src", "ila")
        if not os.path.isdir(sandbox_src):
            sandbox_src = os.path.join(sandbox_path, "ila")
        if not os.path.isdir(sandbox_src):
            sandbox_src = sandbox_path
        if not os.path.isdir(sandbox_src):
            logger.warning("无法检测修改: 沙箱目录不存在 %s", sandbox_src)
            return []

        modified = []
        skip_exts = {".pyc"}
        skip_names = {"AGENTS.md", ".ila-sandbox.json"}

        for root, _dirs, files in os.walk(sandbox_src):
            if "__pycache__" in root:
                continue
            for fname in files:
                if fname in skip_names or os.path.splitext(fname)[1] in skip_exts:
                    continue
                sandbox_file = os.path.join(root, fname)
                rel_path = os.path.relpath(sandbox_file, sandbox_src)
                baseline_file = os.path.join(baseline_path, rel_path)

                # 新文件 → 一定被修改
                if not os.path.exists(baseline_file):
                    modified.append(self._make_module_entry(rel_path))
                    continue

                # 内容对比
                try:
                    with open(sandbox_file, "rb") as sf, open(baseline_file, "rb") as bf:
                        if sf.read() != bf.read():
                            modified.append(self._make_module_entry(rel_path))
                except Exception:
                    pass

        return modified

    @staticmethod
    def _make_module_entry(rel_path: str) -> dict:
        """根据文件路径生成模块标识条目."""
        name = rel_path
        has_visual = rel_path.endswith(".html") or rel_path.endswith(".css")
        # 更友好的名称
        parts = rel_path.replace("/", " › ").replace(".py", "").replace(".html", " (UI)")
        return {"id": rel_path, "name": parts, "has_visual": has_visual}

    def _write_verification_mode(self, modified_modules: list[dict]) -> None:
        """写入验证模式标识文件，始终写入（即使无改动）。"""
        try:
            os.makedirs(os.path.dirname(_VERIFICATION_MODE_FILE), exist_ok=True)
            with open(_VERIFICATION_MODE_FILE, "w") as f:
                json.dump({
                    "enabled": True,
                    "modified_modules": modified_modules,
                    "created_at": time.time(),
                }, f, indent=2)
            logger.info("验证模式已写入: %d 个文件被修改", len(modified_modules))
        except Exception as e:
            logger.warning("写入验证模式文件失败: %s", e)

    def _clear_verification_mode(self) -> None:
        """清除验证模式标识文件."""
        try:
            if os.path.exists(_VERIFICATION_MODE_FILE):
                os.remove(_VERIFICATION_MODE_FILE)
                logger.info("验证模式已关闭")
        except Exception as e:
            logger.warning("清除验证模式文件失败: %s", e)

    @staticmethod
    def _load_verification_mode_static() -> dict | None:
        """静态方法: 读取验证模式文件，供 api.py 调用."""
        try:
            if os.path.exists(_VERIFICATION_MODE_FILE):
                with open(_VERIFICATION_MODE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    # ================================================================
    # 快照与恢复
    # ================================================================

    def create_snapshot(self, obj: ManagedObject) -> str:
        """创建 ILA 源码快照，排除受保护文件.

        Returns:
            快照 tar.gz 文件路径
        """
        snapshot_dir = os.path.expanduser("~/.ila/snapshots/self")
        os.makedirs(snapshot_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = os.path.join(
            snapshot_dir, f"ila-core-{timestamp}.tar.gz"
        )

        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            """排除受保护文件."""
            name = tarinfo.name
            for pattern in _PROTECTED_PATTERNS:
                if pattern in name:
                    return None
            return tarinfo

        try:
            with tarfile.open(snapshot_path, "w:gz") as tar:
                # 打包 src/ila/ 目录
                if os.path.isdir(self.src_dir):
                    tar.add(self.src_dir, arcname="src/ila",
                            filter=_filter)
                # 打包 config/ 目录
                if os.path.isdir(self.config_dir):
                    tar.add(self.config_dir, arcname="config",
                            filter=_filter)

            # 写入元信息
            meta_path = snapshot_path.replace(".tar.gz", ".meta.json")
            with open(meta_path, "w") as f:
                json.dump({
                    "created_at": timestamp,
                    "object_id": obj.object_id,
                    "version": obj.current_version,
                    "project_root": self.project_root,
                    "protected_excludes": _PROTECTED_PATTERNS,
                    "file_count": self._count_files(),
                }, f, indent=2)

            logger.info("ILA 快照已创建: %s (%d files)", snapshot_path,
                        self._count_files())
            return snapshot_path
        except Exception as e:
            logger.error("创建快照失败: %s", e)
            raise

    def _count_files(self) -> int:
        """统计 src/ila/ 下的文件数."""
        count = 0
        if os.path.isdir(self.src_dir):
            for root, _dirs, files in os.walk(self.src_dir):
                # 跳过受保护目录
                if any(p in root for p in _PROTECTED_PATTERNS):
                    continue
                count += len(files)
        return count

    def restore_snapshot(self, obj: ManagedObject, snapshot_path: str) -> bool:
        """从快照恢复 ILA 源码.

        注意: 恢复后需要重启 dashboard 进程才能生效。
        """
        if not os.path.exists(snapshot_path):
            logger.error("快照不存在: %s", snapshot_path)
            return False

        try:
            # 备份当前版本 (快速归档)
            backup_path = f"{self.project_root}/.ila-backup-{int(time.time())}.tar.gz"
            with tarfile.open(backup_path, "w:gz") as tar:
                if os.path.isdir(self.src_dir):
                    tar.add(self.src_dir, arcname="src/ila",
                            filter=lambda x: None if any(
                                p in x.name for p in _PROTECTED_PATTERNS) else x)
                if os.path.isdir(self.config_dir):
                    tar.add(self.config_dir, arcname="config",
                            filter=lambda x: None if any(
                                p in x.name for p in _PROTECTED_PATTERNS) else x)

            # 从快照恢复
            with tarfile.open(snapshot_path, "r:gz") as tar:
                tar.extractall(path=self.project_root)

            logger.info("已从快照恢复: %s (备份: %s)", snapshot_path, backup_path)
            return True
        except Exception as e:
            logger.error("恢复快照失败: %s", e)
            return False

    # ================================================================
    # Staging 部署与调用
    # ================================================================

    def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str:
        """部署新版本到 staging 环境并启动新 ILA 进程.

        流程:
          1. 备份旧文件到 /tmp/ila-staging-backup-*
          2. 复制沙箱文件到 src/ 目录
          3. 启动新 ILA 进程 (port 9528)
          4. 等待健康检查通过

        Args:
            obj: 目标对象 (ila:agent:core)
            sandbox_path: 沙箱工作区路径

        Returns:
            staging_id: staging 实例标识
        """
        staging_id = f"ila-staging-{int(time.time())}"

        # 1. 备份旧文件
        backup_dir = f"/tmp/ila-staging-backup-{int(time.time())}"
        os.makedirs(backup_dir, exist_ok=True)

        # 备份 src/ila/
        if os.path.isdir(self.src_dir):
            shutil.copytree(
                self.src_dir,
                os.path.join(backup_dir, "src", "ila"),
                ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc"),
            )
        # 备份 config/
        if os.path.isdir(self.config_dir):
            shutil.copytree(
                self.config_dir,
                os.path.join(backup_dir, "config"),
            )

        # 检测修改的模块并写入验证模式标识 (通知前端在新版本中高亮修改的模块)
        # 使用备份目录作为比较基准, 确保多次迭代后仍能正确检测变更
        baseline = os.path.join(backup_dir, "src", "ila")
        modified_modules = self._detect_modified_modules(sandbox_path, baseline_path=baseline)
        self._write_verification_mode(modified_modules)

        # 2. 复制沙箱文件到项目目录
        self._copy_sandbox_to_project(sandbox_path)

        # 3. 启动新 ILA 进程 (port 9528)
        new_port = self.staging_port

        # 先清理端口上残留的旧进程
        self._kill_port_processes(new_port)

        # 等待端口释放 (避免竞态条件)
        if not wait_port_free(new_port, timeout=10):
            logger.warning('Staging 端口 %s 超时未释放，尝试继续启动', new_port)

        env = os.environ.copy()
        env["ILA_DASHBOARD_PORT"] = str(new_port)

        # 确保不继承当前进程的端口
        if "ILA_ACTIVE_PORT" in env:
            del env["ILA_ACTIVE_PORT"]

        try:
            process = subprocess.Popen(
                ["python3", "-m", "ila.cli", "dashboard",
                 "--port", str(new_port),
                 "--host", "127.0.0.1"],
                cwd=self.project_root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            # 尝试用 python3.12 或其他版本
            for py in ["python3.12", "python3.11", "python3.10", "python"]:
                try:
                    process = subprocess.Popen(
                        [py, "-m", "ila.cli", "dashboard",
                         "--port", str(new_port),
                         "--host", "127.0.0.1"],
                        cwd=self.project_root,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    break
                except FileNotFoundError:
                    continue
            else:
                raise RuntimeError("找不到可用的 Python 解释器")

        # 4. 健康检查 (等待服务就绪, 使用共享工具)
        health_url = f"http://127.0.0.1:{new_port}/api/status"
        if not health_check(health_url, timeout=30):
            # 健康检查失败: 停止新进程, 恢复旧文件
            logger.warning("新 ILA 进程 (%s) 健康检查失败, 执行回滚", new_port)
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()

            # 恢复旧文件
            self._restore_from_backup(backup_dir)

            raise RuntimeError(
                f"新 ILA 进程 (port {new_port}) 启动后健康检查失败"
            )

        logger.info("新 ILA 进程已就绪: port %s (pid=%d)",
                     new_port, process.pid)

        # 保存 staging 信息
        staging_info = {
            "staging_id": staging_id,
            "port": new_port,
            "pid": process.pid,
            "backup_dir": backup_dir,
            "sandbox_path": sandbox_path,
            "created_at": time.time(),
        }
        self._save_staging_info(staging_id, staging_info)

        return staging_id

    def _copy_sandbox_to_project(self, sandbox_path: str) -> None:
        """将沙箱文件复制到项目目录，保留受保护文件."""
        # 尝试嵌套结构: sandbox/src/ila/
        sandbox_src = os.path.join(sandbox_path, "src", "ila")
        if os.path.isdir(sandbox_src):
            self._merge_dir(sandbox_src, self.src_dir)
        else:
            # 扁平结构: 模块目录直接在 sandbox 根目录下 (Codex sandbox 常见结构)
            # 合并每个模块子目录
            for entry in os.scandir(sandbox_path):
                if entry.is_dir() and not entry.name.startswith("."):
                    src_sub = entry.path
                    dst_sub = os.path.join(self.src_dir, entry.name)
                    if os.path.isdir(dst_sub):
                        self._merge_dir(src_sub, dst_sub)
                    else:
                        # 顶层文件直接复制
                        pass
            # 也复制顶层文件
            for entry in os.scandir(sandbox_path):
                if entry.is_file() and not entry.name.startswith("."):
                    if any(p in entry.name for p in _PROTECTED_PATTERNS):
                        continue
                    shutil.copy2(entry.path, os.path.join(self.src_dir, entry.name))

        # 复制 config/ 下的文件 (但不覆盖 runtime 配置)
        sandbox_config = os.path.join(sandbox_path, "config")
        if os.path.isdir(sandbox_config):
            self._merge_dir(sandbox_config, self.config_dir,
                            skip_existing=["ila_config.yaml"])

    def _merge_dir(self, src: str, dst: str,
                   skip_existing: list[str] | None = None) -> None:
        """合并目录: 将 src 中的文件复制到 dst 中."""
        skip_existing = skip_existing or []
        os.makedirs(dst, exist_ok=True)
        for entry in os.scandir(src):
            name = entry.name
            if name in skip_existing:
                continue
            if any(p in name for p in _PROTECTED_PATTERNS):
                continue
            dst_path = os.path.join(dst, name)
            if entry.is_dir():
                if os.path.exists(dst_path):
                    shutil.rmtree(dst_path)
                shutil.copytree(entry.path, dst_path,
                                ignore=shutil.ignore_patterns(
                                    "__pycache__", "*.pyc"))
            else:
                shutil.copy2(entry.path, dst_path)

    def _restore_from_backup(self, backup_dir: str) -> None:
        """从备份目录恢复旧文件."""
        backup_src = os.path.join(backup_dir, "src", "ila")
        if os.path.isdir(backup_src):
            if os.path.isdir(self.src_dir):
                shutil.rmtree(self.src_dir)
            shutil.copytree(backup_src, self.src_dir)

        backup_config = os.path.join(backup_dir, "config")
        if os.path.isdir(backup_config):
            if os.path.isdir(self.config_dir):
                shutil.rmtree(self.config_dir)
            shutil.copytree(backup_config, self.config_dir)

        logger.info("已从备份恢复: %s", backup_dir)

    def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict[str, Any]:
        """调用当前 ILA (port 9527) 的 API.

        test_input 支持:
          - {"endpoint": "/api/status"}  — 调用指定 API 端点
          - {"run_test": "pytest tests/"} — 运行 pytest 测试套件
          - {"check_file": "core/orchestrator.py", "expect_contains": "class"} — 文件检查
        """
        # 文件检查模式
        check_file = test_input.get("check_file", "")
        if check_file:
            file_path = os.path.join(self.src_dir, check_file)
            if not os.path.exists(file_path):
                return {
                    "output": "", "exit_code": 1,
                    "error": f"文件不存在: {check_file}",
                }
            with open(file_path) as f:
                content = f.read()
            expect_contains = test_input.get("expect_contains", "")
            if expect_contains and expect_contains not in content:
                return {
                    "output": content[:500],
                    "exit_code": 0,
                    "error": f"未找到期望内容: {expect_contains}",
                }
            return {"output": content[:500], "exit_code": 0, "error": ""}

        # pytest 模式
        run_test = test_input.get("run_test", "")
        if run_test:
            try:
                result = subprocess.run(
                    run_test.split(),
                    cwd=self.project_root,
                    capture_output=True, text=True, timeout=180,
                )
                return {
                    "output": result.stdout[-2000:] + result.stderr[-2000:],
                    "exit_code": result.returncode,
                    "error": "" if result.returncode == 0 else result.stderr[:500],
                }
            except subprocess.TimeoutExpired:
                return {"output": "", "exit_code": 1, "error": "pytest 超时 (180s)"}

        # API 调用模式 (默认)
        endpoint = test_input.get("endpoint", "/api/status")
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.dashboard_port}{endpoint}",
                headers={"User-Agent": "ILA-SelfAdapter/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                return {
                    "output": body[:2000],
                    "exit_code": 0,
                    "error": "",
                    "status_code": resp.status,
                }
        except Exception as e:
            return {
                "output": "", "exit_code": 1,
                "error": f"API 调用失败 ({endpoint}): {e}",
            }

    def invoke_staging(self, staging_id: str, test_input: dict) -> dict[str, Any]:
        """调用 staging ILA (port 9528) 的 API.

        与 invoke_object 相同逻辑，但调用 staging 端口。
        """
        info = self._load_staging_info(staging_id)
        if not info:
            return {"output": "", "exit_code": 1, "error": f"staging 不存在: {staging_id}"}

        port = info.get("port", self.staging_port)

        # 文件检查模式
        check_file = test_input.get("check_file", "")
        if check_file:
            file_path = os.path.join(self.src_dir, check_file)
            if not os.path.exists(file_path):
                return {
                    "output": "", "exit_code": 1,
                    "error": f"文件不存在: {check_file}",
                }
            with open(file_path) as f:
                content = f.read()
            expect_contains = test_input.get("expect_contains", "")
            if expect_contains and expect_contains not in content:
                return {
                    "output": content[:500],
                    "exit_code": 0,
                    "error": f"未找到期望内容: {expect_contains}",
                }
            return {"output": content[:500], "exit_code": 0, "error": ""}

        # pytest 模式
        run_test = test_input.get("run_test", "")
        if run_test:
            try:
                result = subprocess.run(
                    run_test.split(),
                    cwd=self.project_root,
                    capture_output=True, text=True, timeout=180,
                )
                return {
                    "output": result.stdout[-2000:] + result.stderr[-2000:],
                    "exit_code": result.returncode,
                    "error": "" if result.returncode == 0 else result.stderr[:500],
                }
            except subprocess.TimeoutExpired:
                return {"output": "", "exit_code": 1, "error": "pytest 超时 (180s)"}

        # API 调用模式
        endpoint = test_input.get("endpoint", "/api/status")
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{endpoint}",
                headers={"User-Agent": "ILA-SelfAdapter/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                return {
                    "output": body[:2000],
                    "exit_code": 0,
                    "error": "",
                    "staging_port": port,
                    "status_code": resp.status,
                }
        except Exception as e:
            return {
                "output": "", "exit_code": 1,
                "error": f"staging API 调用失败 ({endpoint}, port {port}): {e}",
            }

    # ================================================================
    # 热切换
    # ================================================================

    def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict[str, Any]:
        """执行 ILA 自热切换: 零停机替换当前 ILA 进程.

        流程:
          1. 创建快照 (tar.gz)
          2. 部署新文件到 src/ 目录
          3. 启动新 ILA 进程 (port 9528)
          4. 健康检查新进程 (最多 30s)
          5. 通过: 切换流量 iptables/socat → 停止旧进程
          6. 失败: 停止新进程, 恢复旧文件, 保持旧进程运行

        Returns:
            {"status": "success", "snapshot": "..."}
            {"status": "rolled_back", "reason": "...", "snapshot": "..."}
            {"status": "error", "reason": "..."}
        """
        snapshot_path = self.create_snapshot(obj)

        try:
            # 部署到 staging (复制文件 + 启动新进程)
            staging_id = self.deploy_to_staging(obj, sandbox_path)
            info = self._load_staging_info(staging_id)
            if not info:
                raise RuntimeError(f"staging 信息丢失: {staging_id}")
            new_port = info["port"]
            new_pid = info["pid"]

            # 先切换流量 (socat 绑定 9527，利用 reuseaddr 在旧进程还在时共享端口)
            # 再停止旧进程，让迭代线程安全完成
            proxy_pid = self._switch_traffic(new_port)

            # 停止旧进程 (释放 9527)
            # 注意：_stop_current_dashboard 会跳过 proxy_pid 和当前进程，
            # 让 socat 代理和迭代线程安全运行
            skip_pids = {proxy_pid} if proxy_pid else None
            self._stop_current_dashboard(skip_pids=skip_pids)

            # 清理备份
            backup_dir = info.get("backup_dir", "")
            if backup_dir and os.path.isdir(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)

            # 更新 staging 记录
            info["status"] = "active"
            info["switched_at"] = time.time()
            self._save_staging_info(staging_id, info)

            logger.info("ILA 热切换成功: 新进程 port=%s pid=%s",
                        new_port, new_pid)
            return {"status": "success", "snapshot": snapshot_path}

        except Exception as e:
            logger.error("ILA 热切换失败: %s", e)

            # 检查旧进程是否还在运行
            if not self._is_process_running(self.dashboard_port):
                # 旧进程已死，尝试从快照恢复
                logger.warning("旧进程已停止, 尝试从快照恢复...")
                self.restore_snapshot(obj, snapshot_path)
                self._start_dashboard_process(self.dashboard_port)
                time.sleep(3)

            return {
                "status": "rolled_back",
                "reason": str(e),
                "snapshot": snapshot_path,
            }

    def _switch_traffic(self, new_port: int) -> int | None:
        """切换流量从旧端口到新端口.

        优先使用 iptables (需要 root), 回退到 socat 代理。

        Returns:
            socat/代理进程 PID, 或 None (iptables 模式或无进程)
        """
        # 尝试 iptables
        if self._has_root():
            try:
                logger.info("使用 iptables 切换流量: %s → %s",
                            self.dashboard_port, new_port)
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "PREROUTING",
                     "-p", "tcp", "--dport", str(self.dashboard_port),
                     "-j", "REDIRECT", "--to-port", str(new_port)],
                    check=True, timeout=10,
                )
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "OUTPUT",
                     "-p", "tcp", "--dport", str(self.dashboard_port),
                     "-j", "REDIRECT", "--to-port", str(new_port)],
                    check=True, timeout=10,
                )
                return None
            except Exception as e:
                logger.warning("iptables 切换失败: %s, 回退到 socat", e)
        else:
            logger.info("无 root 权限, 使用 socat 代理")

        # 回退: socat 代理
        # 用 socat 监听 9527 转发到 9528
        try:
            logger.info("使用 socat 代理: %s → %s",
                        self.dashboard_port, new_port)
            proc = subprocess.Popen(
                ["socat", f"TCP-LISTEN:{self.dashboard_port},reuseaddr,fork",
                 f"TCP:localhost:{new_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc.pid
        except FileNotFoundError:
            logger.warning("socat 未安装, 使用 Python 实现简单代理")
            # Python 回退: 启动一个简单的 TCP 转发
            self._start_python_proxy(self.dashboard_port, new_port)
            return None

    def _start_python_proxy(self, listen_port: int, target_port: int) -> None:
        """启动一个 Python TCP 转发代理."""
        import socketserver
        import threading

        class ProxyHandler(socketserver.StreamRequestHandler):
            def handle(self):
                import socket
                target_host = "127.0.0.1"
                try:
                    with socket.create_connection(
                            (target_host, target_port), timeout=10) as dst:
                        # 双向转发
                        threads = []
                        for src, dst_sock in [
                            (self.rfile, dst),
                            (dst, self.wfile),
                        ]:
                            def _forward(s, d):
                                try:
                                    data = s.read(65536)
                                    while data:
                                        d.write(data)
                                        d.flush()
                                        data = s.read(65536)
                                except Exception:
                                    pass
                            t = threading.Thread(target=_forward,
                                                 args=(src, dst_sock),
                                                 daemon=True)
                            threads.append(t)
                            t.start()
                        for t in threads:
                            t.join(timeout=30)
                except Exception as e:
                    logger.warning("代理转发异常: %s", e)

        class ThreadedTCPServer(socketserver.ThreadingMixIn,
                                socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = ThreadedTCPServer(("127.0.0.1", listen_port), ProxyHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Python 代理已启动: %s → %s", listen_port, target_port)

    def promote_staging(self, staging_id: str) -> dict[str, Any]:
        """将 staging 版本正式上线到生产端口 (9527) — 通过 Launcher 异步执行.

        由于 promote_staging 通常由 dashboard 自身调用，直接杀旧启新会导致
        当前进程死亡后新进程无法启动。因此委托给独立 Launcher 进程执行：
          1. 写命令文件到 ~/.ila/commands/
          2. 返回 {"status": "promoting"}
          3. Launcher 进程检测到命令后: 杀旧 → 启新 → 健康检查 → 清理 staging

        Args:
            staging_id: deploy_to_staging 返回的 staging 标识

        Returns:
            {"status": "promoting", "command_id": "..."} — 委托成功
            {"status": "error", "reason": "..."}        — 委托失败
        """
        info = self._load_staging_info(staging_id)
        if not info:
            return {"status": "error", "reason": f"staging 不存在: {staging_id}"}

        staging_port = info.get("port", self.staging_port)
        staging_info_file = os.path.expanduser(
            f"~/.ila/staging/{staging_id}.json"
        )

        # 清除验证模式标识 (新版本正式上线后不再需要高亮)
        self._clear_verification_mode()

        # 委托 Launcher 执行重启
        try:
            from ila.launcher_manager import get_launcher_manager

            launcher = get_launcher_manager()
            if not launcher.is_running():
                logger.warning("Launcher 未运行，尝试启动...")
                if not launcher.start():
                    return {"status": "error", "reason": "Launcher 未运行且启动失败"}

            # 获取当前版本号作为旧版本
            try:
                from ila import get_runtime_version
                old_version = get_runtime_version()
            except Exception:
                old_version = None

            result = launcher.send_restart(
                name="ila-dashboard",
                port=self.dashboard_port,
                cmd=[
                    "python3", "-m", "ila.cli", "dashboard",
                    "--port", str(self.dashboard_port),
                    "--host", "0.0.0.0",
                ],
                cwd=self.project_root,
                health_check_url=f"http://127.0.0.1:{self.dashboard_port}/api/status",
                health_check_timeout=30.0,
                cleanup={
                    "staging_info_file": staging_info_file,
                },
                old_version=old_version,
                wait=False,  # 不等待结果 — Launcher 会杀当前进程
            )

            logger.info("已委托 Launcher 执行升级: command_id=%s", result.get("command_id"))
            return result

        except Exception as e:
            logger.error("委托 Launcher 失败: %s", e)
            return {"status": "error", "reason": f"委托 Launcher 失败: {e}"}

    def _kill_port_processes(self, port: int) -> None:
        """强制杀死指定端口上的所有进程."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for pid_str in result.stdout.strip().split():
                    try:
                        pid = int(pid_str)
                        os.kill(pid, signal.SIGKILL)
                        logger.warning("强制清理端口 %s 上的进程: pid=%s", port, pid)
                    except (ProcessLookupError, ValueError, OSError):
                        pass
        except Exception:
            pass

    def _stop_current_dashboard(self, skip_pids: set[int] | None = None) -> None:
        """停止当前的 ILA dashboard 进程.

        Args:
            skip_pids: 要跳过的 PID 集合 (如 socat 代理), 不向这些进程发送信号
        """
        port = self.dashboard_port
        current_pid = os.getpid()
        if skip_pids is None:
            skip_pids = set()
        skip_pids.add(current_pid)
        try:
            # 通过 lsof 找到进程
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = [int(p) for p in result.stdout.strip().split()]
                for pid in pids:
                    if pid in skip_pids:
                        logger.info("跳过进程 %d (保留代理/当前进程)", pid)
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                        logger.info("已发送 SIGTERM 到进程 %d", pid)
                    except ProcessLookupError:
                        continue

                # 等待进程结束 (跳过 skip_pids)
                for _ in range(3):
                    time.sleep(1)
                    still_alive = []
                    for pid in pids:
                        if pid in skip_pids:
                            continue
                        try:
                            os.kill(pid, 0)
                            still_alive.append(pid)
                        except ProcessLookupError:
                            pass
                    if not still_alive:
                        return
                    pids = still_alive

                # 强制杀死 (跳过 skip_pids，让迭代线程和代理安全运行)
                for pid in pids:
                    if pid in skip_pids:
                        logger.info("跳过进程 %d (保留代理/当前进程)", pid)
                        continue
                    try:
                        os.kill(pid, signal.SIGKILL)
                        logger.warning("已强制杀死进程 %d", pid)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            logger.warning("停止旧进程失败: %s", e)

    def _is_process_running(self, port: int) -> bool:
        """检查指定端口是否有进程在运行."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0 and result.stdout.strip() != ""
        except Exception:
            return False

    def _start_dashboard_process(self, port: int) -> subprocess.Popen | None:
        """启动 ILA dashboard 进程."""
        # 先清理端口上残留的旧进程
        self._kill_port_processes(port)
        try:
            proc = subprocess.Popen(
                ["python3", "-m", "ila.cli", "dashboard",
                 "--port", str(port), "--host", "127.0.0.1"],
                cwd=self.project_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc
        except Exception as e:
            logger.error("启动 dashboard 失败: %s", e)
            return None

    def _has_root(self) -> bool:
        """检查是否有 root 权限."""
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    # ================================================================
    # 健康检查与重载
    # ================================================================

    def health_check(self, obj: ManagedObject) -> bool:
        """ILA 健康检查: 检查 API 是否响应 + 文件完整性.

        检查两个端口 (9527 和 9528) 中任意一个可用即可。
        """
        for port in [self.dashboard_port, self.staging_port]:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/status",
                    headers={"User-Agent": "ILA-SelfAdapter/1.0"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        # 验证响应内容
                        body = resp.read().decode("utf-8")
                        data = json.loads(body)
                        if "platforms_registered" in data:
                            return True
            except Exception:
                continue

        # 如果 API 不可用, 做文件级验证
        return self._verify_file_integrity()

    def _verify_file_integrity(self) -> bool:
        """验证核心文件完整性."""
        required = [
            "cli.py",
            "core/__init__.py",
            "core/orchestrator.py",
            "adapters/__init__.py",
            "adapters/base.py",
            "adapters/registry.py",
            "models/managed_object.py",
        ]
        for rel_path in required:
            full_path = os.path.join(self.src_dir, rel_path)
            if not os.path.exists(full_path):
                logger.warning("完整性检查失败: %s 不存在", rel_path)
                return False
        return True

    def reload(self, obj: ManagedObject) -> bool:
        """ILA 重载验证: 检查文件完整性 + 核心结构.

        对于 ILA 自升级, 重载意味着新 session 会加载新代码。
        这里做文件完整性验证 (因为当前进程已加载旧代码, 新进程才会用新代码)。
        """
        if not self._verify_file_integrity():
            return False

        # 额外检查: 确保核心模块可导入
        try:
            import sys
            sys.path.insert(0, os.path.join(self.project_root, "src"))
            # 尝试导入 post-upgrade 关键模块
            for mod in ["ila.cli", "ila.core.orchestrator",
                        "ila.adapters.registry"]:
                try:
                    __import__(mod)
                except ImportError as e:
                    logger.warning("模块导入检查失败: %s — %s", mod, e)
                    return False
            return True
        except Exception as e:
            logger.warning("重载验证失败: %s", e)
            return False

    # ================================================================
    # 文件与兼容性
    # ================================================================

    def get_object_files(self, obj: ManagedObject) -> list[str]:
        """获取 ILA 源码目录中的所有文件列表."""
        if not os.path.isdir(self.src_dir):
            return []
        files = []
        for root, dirs, filenames in os.walk(self.src_dir):
            # 跳过受保护目录
            dirs[:] = [d for d in dirs if d not in _PROTECTED_PATTERNS]
            for fname in filenames:
                if fname.endswith(".pyc"):
                    continue
                fpath = os.path.join(root, fname)
                files.append(fpath)
        return files

    def validate_compatibility(self, obj: ManagedObject,
                               sandbox_path: str) -> dict[str, Any]:
        """验证新版本 ILA 的兼容性.

        检查:
          1. 核心文件结构是否完整
          2. sandbox 中是否有必要的入口文件
          3. 是否缺少关键文件
        """
        issues: list[str] = []
        warnings: list[str] = []

        # 1. 检查核心文件结构
        required = ["cli.py", "core/orchestrator.py",
                    "adapters/base.py", "adapters/registry.py",
                    "models/managed_object.py"]
        for rel_path in required:
            sandbox_file = os.path.join(sandbox_path, rel_path)
            if not os.path.exists(sandbox_file):
                issues.append(f"沙箱缺少核心文件: {rel_path}")

        # 2. 检查可导入性
        try:
            import sys
            sandbox_parent = os.path.dirname(sandbox_path)
            if sandbox_parent not in sys.path:
                sys.path.insert(0, sandbox_parent)
            # 尝试导入 sandbox 中的 cli 模块
            sandbox_ila = os.path.join(sandbox_parent, "ila")
            if os.path.isdir(sandbox_ila):
                init_file = os.path.join(sandbox_ila, "__init__.py")
                if not os.path.exists(init_file):
                    warnings.append("沙箱中 ila/__init__.py 缺失")
        except Exception as e:
            warnings.append(f"沙箱导入检查异常: {e}")

        # 3. 检查文件变更
        old_files = set()
        for f in self.get_object_files(obj):
            rel = os.path.relpath(f, self.src_dir)
            old_files.add(rel)

        new_files = set()
        for root, _dirs, filenames in os.walk(sandbox_path):
            for fname in filenames:
                if fname.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(root, fname), sandbox_path)
                new_files.add(rel)

        removed = old_files - new_files
        if removed:
            warnings.append(f"新版本缺少以下文件: {removed}")

        # 4. 检查受保护文件是否被修改
        protected = ["registry.py", "base.py", "managed_object.py"]
        for rel_path in protected:
            old_file = os.path.join(self.src_dir, rel_path)
            new_file = os.path.join(sandbox_path, rel_path)
            if os.path.exists(old_file) and os.path.exists(new_file):
                old_content = open(old_file).read()
                new_content = open(new_file).read()
                if old_content != new_content:
                    warnings.append(
                        f"受保护文件被修改: {rel_path} "
                        f"(请确认修改是安全的)"
                    )

        compatible = len(issues) == 0
        return {"compatible": compatible, "issues": issues, "warnings": warnings}

    # ================================================================
    # Staging 信息管理
    # ================================================================

    def _save_staging_info(self, staging_id: str, info: dict) -> None:
        """保存 staging 信息到文件."""
        info_path = os.path.join(_STAGING_DIR, f"{staging_id}.json")
        with open(info_path, "w") as f:
            json.dump(info, f, indent=2)

    def _load_staging_info(self, staging_id: str) -> dict | None:
        """加载 staging 信息."""
        info_path = os.path.join(_STAGING_DIR, f"{staging_id}.json")
        if not os.path.exists(info_path):
            return None
        try:
            with open(info_path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("加载 staging 信息失败: %s", e)
            return None

    def cleanup_staging(self, staging_id: str) -> None:
        """清理 staging 环境.

        停止 staging 进程, 删除 staging 信息文件。
        同时强制清理 staging 端口上的所有残留进程。
        """
        info = self._load_staging_info(staging_id)
        if info:
            pid = info.get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info("已停止 staging 进程: pid=%s", pid)
                except ProcessLookupError:
                    pass

            info_path = os.path.join(_STAGING_DIR, f"{staging_id}.json")
            if os.path.exists(info_path):
                os.remove(info_path)

            backup_dir = info.get("backup_dir", "")
            if backup_dir and os.path.isdir(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)

        # 强制清理 staging 端口上的任何残留进程
        port = self.staging_port
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for pid in result.stdout.strip().split():
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                        logger.warning("强制清理端口 %s 上的残留进程: pid=%s", port, pid)
                    except (ProcessLookupError, ValueError):
                        pass
        except Exception:
            pass