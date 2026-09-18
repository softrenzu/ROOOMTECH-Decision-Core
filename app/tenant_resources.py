from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone


RESOURCE_TYPES = {"dataset", "review", "model"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TenantResourceRegistry:
    """Bind persisted resource IDs to exactly one enterprise project.

    The registry shares the enterprise control-plane database so project deletion can
    cascade ownership rows. Project-facing APIs must check this registry before reading,
    mutating, training, exporting, or invoking a persisted resource.

    Cross-project lookups deliberately raise FileNotFoundError so callers cannot use
    resource IDs to enumerate another project's assets.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_ENTERPRISE_DB", "data/enterprise.sqlite3")
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_resources (
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(resource_type, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_project_resources_project
                    ON project_resources(project_id, resource_type, created_at);
                """
            )

    @staticmethod
    def _type(resource_type: str) -> str:
        value = str(resource_type).strip().lower()
        if value not in RESOURCE_TYPES:
            raise ValueError(f"unsupported tenant resource type: {resource_type}")
        return value

    def _require_project(self, project_id: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND enabled = 1", (project_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"project not found or disabled: {project_id}")

    def register_resource(self, project_id: str, resource_type: str, resource_id: str) -> None:
        resource_type = self._type(resource_type)
        resource_id = str(resource_id).strip()
        if not resource_id:
            raise ValueError("resource_id is required")
        self._require_project(project_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT project_id FROM project_resources WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
            if row is not None:
                if row["project_id"] != project_id:
                    raise PermissionError("resource is already owned by another project")
                return
            self._conn.execute(
                "INSERT INTO project_resources(resource_type, resource_id, project_id, created_at) VALUES (?, ?, ?, ?)",
                (resource_type, resource_id, project_id, _now()),
            )

    def assert_owner(self, project_id: str, resource_type: str, resource_id: str) -> None:
        resource_type = self._type(resource_type)
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id FROM project_resources WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
        if row is None or row["project_id"] != project_id:
            raise FileNotFoundError(f"{resource_type} resource not found")

    def owner(self, resource_type: str, resource_id: str) -> str | None:
        resource_type = self._type(resource_type)
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id FROM project_resources WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
        return None if row is None else str(row["project_id"])

    def list_ids(self, project_id: str, resource_type: str, limit: int = 1000) -> list[str]:
        resource_type = self._type(resource_type)
        self._require_project(project_id)
        limit = max(1, min(int(limit), 50_000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT resource_id FROM project_resources
                WHERE project_id = ? AND resource_type = ?
                ORDER BY created_at DESC, resource_id DESC LIMIT ?
                """,
                (project_id, resource_type, limit),
            ).fetchall()
        return [str(row["resource_id"]) for row in rows]

    def unregister_resource(self, project_id: str, resource_type: str, resource_id: str) -> bool:
        resource_type = self._type(resource_type)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM project_resources WHERE project_id = ? AND resource_type = ? AND resource_id = ?",
                (project_id, resource_type, resource_id),
            )
            return bool(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
