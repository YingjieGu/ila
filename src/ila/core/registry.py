"""Version Registry: 版本注册表 — SQLite 持久化所有迭代元数据."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Any

from ila.models.managed_object import ManagedObject


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS platforms (
    platform_id  TEXT PRIMARY KEY,
    adapter_class TEXT NOT NULL,
    config_path  TEXT,
    enabled      BOOLEAN DEFAULT 1,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS objects (
    object_id       TEXT PRIMARY KEY,
    platform        TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    object_name     TEXT NOT NULL,
    object_path     TEXT NOT NULL,
    current_version TEXT NOT NULL DEFAULT 'unknown',
    metadata        TEXT,
    FOREIGN KEY (platform) REFERENCES platforms(platform_id)
);

CREATE TABLE IF NOT EXISTS versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id           TEXT NOT NULL,
    iter_version        INTEGER NOT NULL DEFAULT 1,
    version             TEXT NOT NULL,
    sandbox_path        TEXT,
    status              TEXT DEFAULT 'developing',
    task_spec           TEXT,
    test_results        TEXT,
    deploy_verification TEXT,
    rollback_snapshot   TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deployed_at         TIMESTAMP,
    FOREIGN KEY (object_id) REFERENCES objects(object_id)
);

CREATE TABLE IF NOT EXISTS test_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id       TEXT NOT NULL,
    test_type       TEXT NOT NULL,
    test_input      TEXT NOT NULL,
    expected_output TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (object_id) REFERENCES objects(object_id)
);

CREATE TABLE IF NOT EXISTS ila_self_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    version           TEXT NOT NULL,
    change_description TEXT,
    sandbox_path      TEXT,
    status            TEXT DEFAULT 'developing',
    review_status     TEXT DEFAULT 'pending',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class VersionRegistry:
    """版本注册表 — 管理 ILA 所有迭代元数据.

    所有数据存储在 SQLite 数据库中，无外部依赖。
    """

    def __init__(self, ila_home: str = "~/.ila"):
        """初始化版本注册表.

        Args:
            ila_home: ILA 数据目录路径 (默认 ``~/.ila``)
        """
        self.ila_home = os.path.expanduser(ila_home)
        os.makedirs(self.ila_home, exist_ok=True)
        self.db_path = os.path.join(self.ila_home, "registry.db")
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库 schema."""
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """迁移已有数据库 schema，添加缺失的列."""
        with self._connect() as conn:
            # v1.4.0+: 添加 iter_version 列
            try:
                conn.execute(
                    "ALTER TABLE versions ADD COLUMN iter_version INTEGER NOT NULL DEFAULT 1"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ---- Platform ----

    def register_platform(self, platform_id: str, adapter_class: str,
                          config_path: str = "", enabled: bool = True) -> None:
        """注册平台适配器."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO platforms (platform_id, adapter_class, config_path, enabled)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(platform_id) DO UPDATE SET
                     adapter_class=excluded.adapter_class,
                     config_path=excluded.config_path,
                     enabled=excluded.enabled""",
                (platform_id, adapter_class, config_path, enabled),
            )

    def get_platforms(self) -> list[dict[str, Any]]:
        """获取所有已注册平台."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM platforms").fetchall()
            return [dict(r) for r in rows]

    # ---- Object ----

    def register_object(self, obj: ManagedObject) -> None:
        """注册或更新被纳管对象.

        如果对象的平台尚未注册，会自动创建平台记录。
        """
        with self._connect() as conn:
            # 自动确保平台记录存在 (不覆盖已注册的适配器信息)
            conn.execute(
                """INSERT INTO platforms (platform_id, adapter_class, config_path, enabled)
                   VALUES (?, 'auto', '', 1)
                   ON CONFLICT(platform_id) DO NOTHING""",
                (obj.platform,),
            )
            conn.execute(
                """INSERT INTO objects (object_id, platform, object_type, object_name,
                                        object_path, current_version, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(object_id) DO UPDATE SET
                     platform=excluded.platform,
                     object_type=excluded.object_type,
                     object_name=excluded.object_name,
                     object_path=excluded.object_path,
                     current_version=CASE
                       WHEN objects.current_version = 'unknown' OR objects.current_version IS NULL
                         THEN excluded.current_version
                       ELSE objects.current_version
                     END,
                     metadata=excluded.metadata""",
                (
                    obj.object_id,
                    obj.platform,
                    obj.object_type,
                    obj.name,
                    obj.path,
                    obj.current_version,
                    json.dumps(obj.metadata, ensure_ascii=False),
                ),
            )

    def get_object(self, object_id: str) -> dict[str, Any] | None:
        """获取指定对象."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM objects WHERE object_id = ?", (object_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d["metadata"] = json.loads(d.get("metadata") or "{}")
                return d
            return None

    def get_all_objects(self, platform: str | None = None) -> list[dict[str, Any]]:
        """获取所有对象，可按平台过滤."""
        with self._connect() as conn:
            if platform:
                rows = conn.execute(
                    "SELECT * FROM objects WHERE platform = ?", (platform,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects").fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["metadata"] = json.loads(d.get("metadata") or "{}")
                result.append(d)
            return result

    def update_object_version(self, object_id: str, version: str) -> None:
        """更新对象当前版本号."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE objects SET current_version = ? WHERE object_id = ?",
                (version, object_id),
            )

    def delete_object(self, object_id: str) -> None:
        """删除对象."""
        with self._connect() as conn:
            conn.execute("DELETE FROM objects WHERE object_id = ?", (object_id,))

    # ---- Version ----

    @staticmethod
    def _parse_semver_parts(version: str) -> tuple:
        """Parse a semantic version string into comparable tuple.

        Examples:
            "1.4.10"  -> (1, 4, 10)
            "v1.4.3"  -> (1, 4, 3)
            "1.0"     -> (1, 0, 0)
            "12"      -> (12,)

        Returns an empty tuple for unparseable versions.
        """
        v = str(version).lstrip("v")
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, TypeError):
            return ()

    @staticmethod
    def _bump_patch(version: str) -> str:
        """Bump the patch component of a semantic version string.

        Examples:
            "1.4.0" -> "1.4.1"
            "v1.4.3" -> "1.4.4"
            "1.0"   -> "1.0.1"
        """
        v = version.lstrip("v")
        parts = v.split(".")
        if len(parts) >= 3:
            try:
                parts[-1] = str(int(parts[-1]) + 1)
            except ValueError:
                parts.append("1")
        else:
            parts.append("1")
        return ".".join(parts)

    def _compute_iter_version(self, conn, object_id: str) -> str:
        """Compute the next iteration version as a semantic version string.

        Uses the object's current_version as the base and bumps the patch.
        Handles legacy INTEGER iter_version values gracefully.

        IMPORTANT: Uses Python-side semver comparison instead of SQLite MAX
        because SQLite's MAX on TEXT strings uses lexicographic ordering,
        which would order "1.4.9" after "1.4.10" incorrectly.
        """
        # Get object's current_version
        obj_row = conn.execute(
            "SELECT current_version FROM objects WHERE object_id = ?",
            (object_id,),
        ).fetchone()
        base_version = obj_row["current_version"] if obj_row else "0.1.0"
        if base_version == "unknown":
            base_version = "0.1.0"

        # Fetch all existing iter_version values for proper semver comparison
        rows = conn.execute(
            "SELECT iter_version FROM versions WHERE object_id = ? AND iter_version IS NOT NULL",
            (object_id,),
        ).fetchall()

        max_iter_str = None

        if rows:
            # Find the true maximum using semantic version comparison
            best_parts = ()
            for row in rows:
                raw = str(row["iter_version"])
                # Skip legacy integer-only values (no dots) when we have semvers
                if "." not in raw and any("." in str(r["iter_version"]) for r in rows):
                    continue
                parts = self._parse_semver_parts(raw)
                if not parts and raw.isdigit():
                    # Legacy integer: treat as patch number from base
                    b = base_version.lstrip("v").split(".")
                    if len(b) >= 3:
                        parts = (int(b[0]), int(b[1]), int(raw))
                    else:
                        parts = (0, 1, int(raw))
                if parts > best_parts:
                    best_parts = parts
                    max_iter_str = ".".join(str(p) for p in parts)

            if max_iter_str is not None:
                return self._bump_patch(max_iter_str)

        # No existing versions or all legacy — bump from base
        return self._bump_patch(base_version)

    def get_next_version(self, object_id: str) -> str:
        """计算对象的下一个语义版本号.

        基于 current_version 递增补丁号。
        """
        with self._connect() as conn:
            return self._compute_iter_version(conn, object_id)

    def create_version(self, object_id: str, version: str,
                       sandbox_path: str = "", task_spec: dict | None = None) -> int:
        """创建新版本记录.

        自动计算迭代版本号: 基于托管对象的 current_version,
        递增补丁号 (例如 "1.4.0" → "1.4.1" → "1.4.2").
        兼容旧的整数计数器方式.

        Returns:
            版本记录 ID
        """
        with self._connect() as conn:
            next_iter = self._compute_iter_version(conn, object_id)

            cursor = conn.execute(
                """INSERT INTO versions (iter_version, object_id, version, sandbox_path, task_spec, status)
                   VALUES (?, ?, ?, ?, ?, 'developing')""",
                (
                    next_iter,
                    object_id,
                    version,
                    sandbox_path,
                    json.dumps(task_spec, ensure_ascii=False) if task_spec else None,
                ),
            )
            return cursor.lastrowid

    def update_version_status(self, version_id: int, status: str,
                              test_results: dict | None = None,
                              deploy_verification: dict | None = None,
                              rollback_snapshot: str | None = None) -> None:
        """更新版本状态和关联数据."""
        updates = ["status = ?"]
        params: list[Any] = [status]

        if test_results is not None:
            updates.append("test_results = ?")
            params.append(json.dumps(test_results, ensure_ascii=False))
        if deploy_verification is not None:
            updates.append("deploy_verification = ?")
            params.append(json.dumps(deploy_verification, ensure_ascii=False))
        if rollback_snapshot is not None:
            updates.append("rollback_snapshot = ?")
            params.append(rollback_snapshot)
        if status == "live":
            updates.append("deployed_at = ?")
            params.append(datetime.now().isoformat())

        params.append(version_id)
        sql = f"UPDATE versions SET {', '.join(updates)} WHERE id = ?"
        with self._connect() as conn:
            conn.execute(sql, params)
            # 当状态变为 live 时，同步更新纳管对象的 current_version
            if status == "live":
                version_row = conn.execute(
                    "SELECT object_id, version FROM versions WHERE id = ?",
                    (version_id,),
                ).fetchone()
                if version_row:
                    conn.execute(
                        "UPDATE objects SET current_version = ? WHERE object_id = ?",
                        (version_row["version"], version_row["object_id"]),
                    )

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        """获取指定版本记录."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row:
                d = dict(row)
                for key in ("task_spec", "test_results", "deploy_verification"):
                    if d.get(key):
                        d[key] = json.loads(d[key])
                return d
            return None

    def get_versions_by_object(self, object_id: str) -> list[dict[str, Any]]:
        """获取对象的所有版本记录."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM versions WHERE object_id = ? ORDER BY created_at DESC, id DESC",
                (object_id,),
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                for key in ("task_spec", "test_results", "deploy_verification"):
                    if d.get(key):
                        d[key] = json.loads(d[key])
                result.append(d)
            return result

    def get_latest_version(self, object_id: str) -> dict[str, Any] | None:
        """获取对象最新的版本记录."""
        versions = self.get_versions_by_object(object_id)
        return versions[0] if versions else None

    def get_snapshot_path(self, object_id: str, version: str) -> str | None:
        """获取指定版本的回滚快照路径."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT rollback_snapshot FROM versions
                   WHERE object_id = ? AND version = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (object_id, version),
            ).fetchone()
            return row["rollback_snapshot"] if row and row["rollback_snapshot"] else None

    # ---- Test Cases ----

    def add_test_case(self, object_id: str, test_type: str,
                      test_input: dict, expected_output: dict | None = None) -> int:
        """添加测试用例."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO test_cases (object_id, test_type, test_input, expected_output)
                   VALUES (?, ?, ?, ?)""",
                (
                    object_id,
                    test_type,
                    json.dumps(test_input, ensure_ascii=False),
                    json.dumps(expected_output, ensure_ascii=False) if expected_output else None,
                ),
            )
            return cursor.lastrowid

    def get_test_cases(self, object_id: str,
                       test_type: str | None = None) -> list[dict[str, Any]]:
        """获取对象的测试用例."""
        with self._connect() as conn:
            if test_type:
                rows = conn.execute(
                    "SELECT * FROM test_cases WHERE object_id = ? AND test_type = ?",
                    (object_id, test_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM test_cases WHERE object_id = ?", (object_id,)
                ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["test_input"] = json.loads(d["test_input"])
                if d.get("expected_output"):
                    d["expected_output"] = json.loads(d["expected_output"])
                result.append(d)
            return result

    # ---- ILA Self-Evolution ----

    def create_self_version(self, version: str, change_description: str,
                            sandbox_path: str = "") -> int:
        """创建 ILA 自身版本记录."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO ila_self_versions (version, change_description, sandbox_path, status)
                   VALUES (?, ?, ?, 'developing')""",
                (version, change_description, sandbox_path),
            )
            return cursor.lastrowid

    def update_self_version_status(self, version_id: int, status: str,
                                   review_status: str = "") -> None:
        """更新 ILA 自身版本状态."""
        with self._connect() as conn:
            if review_status:
                conn.execute(
                    """UPDATE ila_self_versions SET status = ?, review_status = ?
                       WHERE id = ?""",
                    (status, review_status, version_id),
                )
            else:
                conn.execute(
                    "UPDATE ila_self_versions SET status = ? WHERE id = ?",
                    (status, version_id),
                )

    def get_self_versions(self) -> list[dict[str, Any]]:
        """获取 ILA 自身所有版本记录."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ila_self_versions ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- Stats ----

    def get_stats(self) -> dict[str, Any]:
        """获取注册表统计信息."""
        with self._connect() as conn:
            platforms = conn.execute("SELECT COUNT(*) FROM platforms").fetchone()[0]
            objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            live_versions = conn.execute(
                "SELECT COUNT(*) FROM versions WHERE status = 'live'"
            ).fetchone()[0]
            test_cases = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
            self_versions = conn.execute(
                "SELECT COUNT(*) FROM ila_self_versions"
            ).fetchone()[0]
            return {
                "platforms": platforms,
                "objects": objects,
                "total_versions": versions,
                "live_versions": live_versions,
                "test_cases": test_cases,
                "self_versions": self_versions,
            }
