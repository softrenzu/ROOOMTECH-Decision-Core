from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.enterprise_models import (
    AuditEvent,
    AuthContext,
    ModelDeploymentSummary,
    ProjectCreate,
    ProjectKeyCreate,
    ProjectKeyIssued,
    ProjectKeySummary,
    ProjectSummary,
    ProjectUpdate,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day() -> str:
    return _now().date().isoformat()


class EnterpriseStore:
    """SQLite reference control plane for projects, scoped keys, quota, audit and model promotion.

    Raw API keys are never persisted. The bundled store is intended for local/single-node
    deployments. Enterprise deployments should use managed storage and a centralized auth
    layer appropriate to their availability and compliance requirements.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_ENTERPRISE_DB", "data/enterprise.sqlite3")
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.pepper = os.getenv("RTDC_ENTERPRISE_KEY_PEPPER", "").encode("utf-8")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_quota_per_day INTEGER NOT NULL,
                    enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_keys (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    prefix TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_daily (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    day TEXT NOT NULL,
                    requests INTEGER NOT NULL,
                    PRIMARY KEY(project_id, day)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    project_id TEXT,
                    key_id TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    request_id TEXT
                );
                CREATE TABLE IF NOT EXISTS model_deployments (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    decision_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    promoted_at TEXT NOT NULL,
                    promoted_by_key_id TEXT,
                    note TEXT,
                    PRIMARY KEY(project_id, decision_id, environment)
                );
                CREATE TABLE IF NOT EXISTS model_deployment_history (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    promoted_at TEXT NOT NULL,
                    promoted_by_key_id TEXT,
                    note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_keys_project ON project_keys(project_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_project_time ON audit_events(project_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_deploy_history ON model_deployment_history(project_id, decision_id, environment, version);
                """
            )

    def _hash_key(self, token: str) -> str:
        raw = token.encode("utf-8")
        if self.pepper:
            return hmac.new(self.pepper, raw, hashlib.sha256).hexdigest()
        return hashlib.sha256(raw).hexdigest()

    def _usage(self, project_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT requests FROM usage_daily WHERE project_id = ? AND day = ?",
                (project_id, _day()),
            ).fetchone()
        return 0 if row is None else int(row["requests"])

    def _project_row(self, row: sqlite3.Row) -> ProjectSummary:
        return ProjectSummary(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            request_quota_per_day=int(row["request_quota_per_day"]),
            enabled=bool(row["enabled"]),
            requests_today=self._usage(row["id"]),
        )

    def create_project(self, request: ProjectCreate) -> ProjectSummary:
        now = _now().isoformat()
        project_id = "prj_" + uuid.uuid4().hex[:20]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, request.name, now, now, request.request_quota_per_day, int(request.enabled)),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> ProjectSummary:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"project not found: {project_id}")
        return self._project_row(row)

    def list_projects(self, limit: int = 100) -> list[ProjectSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        return [self._project_row(row) for row in rows]

    def update_project(self, project_id: str, request: ProjectUpdate) -> ProjectSummary:
        current = self.get_project(project_id)
        name = current.name if request.name is None else request.name
        quota = current.request_quota_per_day if request.request_quota_per_day is None else request.request_quota_per_day
        enabled = current.enabled if request.enabled is None else request.enabled
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE projects SET name = ?, request_quota_per_day = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (name, quota, int(enabled), _now().isoformat(), project_id),
            )
        return self.get_project(project_id)

    def issue_key(self, project_id: str, request: ProjectKeyCreate) -> ProjectKeyIssued:
        self.get_project(project_id)
        key_id = "key_" + uuid.uuid4().hex[:20]
        prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        token = f"rtdc_pk_{prefix}_{secret}"
        now = _now()
        expires_at = None if request.expires_days is None else (now + timedelta(days=request.expires_days)).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO project_keys
                (id, project_id, name, key_hash, prefix, scopes_json, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    key_id,
                    project_id,
                    request.name,
                    self._hash_key(token),
                    prefix,
                    json.dumps(request.scopes, separators=(",", ":")),
                    now.isoformat(),
                    expires_at,
                ),
            )
        return ProjectKeyIssued(
            id=key_id,
            project_id=project_id,
            name=request.name,
            prefix=prefix,
            scopes=list(request.scopes),
            created_at=now.isoformat(),
            expires_at=expires_at,
            revoked_at=None,
            key=token,
        )

    @staticmethod
    def _key_summary(row: sqlite3.Row) -> ProjectKeySummary:
        return ProjectKeySummary(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            prefix=row["prefix"],
            scopes=json.loads(row["scopes_json"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def list_keys(self, project_id: str, limit: int = 100) -> list[ProjectKeySummary]:
        self.get_project(project_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM project_keys WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._key_summary(row) for row in rows]

    def revoke_key(self, project_id: str, key_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE project_keys SET revoked_at = ? WHERE id = ? AND project_id = ? AND revoked_at IS NULL",
                (_now().isoformat(), key_id, project_id),
            )
            return bool(cursor.rowcount)

    def authenticate(self, token: str, required_scope: str | None = None, consume_quota: bool = True) -> AuthContext:
        if not token:
            raise PermissionError("missing project API key")
        digest = self._hash_key(token)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT k.*, p.enabled AS project_enabled, p.request_quota_per_day
                FROM project_keys k JOIN projects p ON p.id = k.project_id
                WHERE k.key_hash = ?
                """,
                (digest,),
            ).fetchone()
        if row is None:
            raise PermissionError("invalid project API key")
        if not bool(row["project_enabled"]):
            raise PermissionError("project is disabled")
        if row["revoked_at"]:
            raise PermissionError("project API key is revoked")
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= _now():
            raise PermissionError("project API key is expired")
        scopes = json.loads(row["scopes_json"])
        if required_scope and required_scope not in scopes:
            raise PermissionError(f"project API key lacks scope: {required_scope}")

        quota = int(row["request_quota_per_day"])
        requests_today = self._usage(row["project_id"])
        if consume_quota:
            with self._lock, self._conn:
                requests_today = self._usage(row["project_id"])
                if requests_today >= quota:
                    raise OverflowError("daily project request quota exceeded")
                self._conn.execute(
                    """
                    INSERT INTO usage_daily(project_id, day, requests) VALUES (?, ?, 1)
                    ON CONFLICT(project_id, day) DO UPDATE SET requests = requests + 1
                    """,
                    (row["project_id"], _day()),
                )
                requests_today += 1
        return AuthContext(
            project_id=row["project_id"],
            key_id=row["id"],
            scopes=scopes,
            request_quota_per_day=quota,
            requests_today=requests_today,
        )

    def audit(
        self,
        *,
        project_id: str | None,
        key_id: str | None,
        method: str,
        path: str,
        status_code: int,
        latency_ms: float,
        request_id: str | None,
    ) -> AuditEvent:
        event = AuditEvent(
            id="aud_" + uuid.uuid4().hex[:20],
            created_at=_now().isoformat(),
            project_id=project_id,
            key_id=key_id,
            method=method,
            path=path,
            status_code=status_code,
            latency_ms=round(latency_ms, 3),
            request_id=request_id,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.created_at,
                    event.project_id,
                    event.key_id,
                    event.method,
                    event.path,
                    event.status_code,
                    event.latency_ms,
                    event.request_id,
                ),
            )
        return event

    def list_audit(self, project_id: str | None = None, limit: int = 200) -> list[AuditEvent]:
        limit = max(1, min(limit, 5000))
        with self._lock:
            if project_id:
                rows = self._conn.execute(
                    "SELECT * FROM audit_events WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [AuditEvent(**dict(row)) for row in rows]

    def promote_model(
        self,
        project_id: str,
        decision_id: str,
        environment: str,
        model_id: str,
        key_id: str | None,
        note: str | None,
    ) -> ModelDeploymentSummary:
        self.get_project(project_id)
        now = _now().isoformat()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM model_deployments WHERE project_id = ? AND decision_id = ? AND environment = ?",
                (project_id, decision_id, environment),
            ).fetchone()
            version = 1 if existing is None else int(existing["version"]) + 1
            if existing is not None:
                self._conn.execute(
                    "INSERT INTO model_deployment_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "dep_" + uuid.uuid4().hex[:20],
                        project_id,
                        decision_id,
                        environment,
                        existing["model_id"],
                        existing["version"],
                        existing["promoted_at"],
                        existing["promoted_by_key_id"],
                        existing["note"],
                    ),
                )
            self._conn.execute(
                """
                INSERT INTO model_deployments(project_id, decision_id, environment, model_id, version, promoted_at, promoted_by_key_id, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, decision_id, environment) DO UPDATE SET
                  model_id=excluded.model_id, version=excluded.version, promoted_at=excluded.promoted_at,
                  promoted_by_key_id=excluded.promoted_by_key_id, note=excluded.note
                """,
                (project_id, decision_id, environment, model_id, version, now, key_id, note),
            )
        return self.get_deployment(project_id, decision_id, environment)

    def get_deployment(self, project_id: str, decision_id: str, environment: str) -> ModelDeploymentSummary:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM model_deployments WHERE project_id = ? AND decision_id = ? AND environment = ?",
                (project_id, decision_id, environment),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("model deployment not found")
        return ModelDeploymentSummary(**dict(row))

    def list_deployments(self, project_id: str) -> list[ModelDeploymentSummary]:
        self.get_project(project_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM model_deployments WHERE project_id = ? ORDER BY decision_id, environment",
                (project_id,),
            ).fetchall()
        return [ModelDeploymentSummary(**dict(row)) for row in rows]

    def rollback_model(self, project_id: str, decision_id: str, environment: str, key_id: str | None) -> tuple[ModelDeploymentSummary, str]:
        current = self.get_deployment(project_id, decision_id, environment)
        with self._lock:
            previous = self._conn.execute(
                """
                SELECT * FROM model_deployment_history
                WHERE project_id = ? AND decision_id = ? AND environment = ?
                ORDER BY version DESC LIMIT 1
                """,
                (project_id, decision_id, environment),
            ).fetchone()
        if previous is None:
            raise FileNotFoundError("no previous model deployment to roll back to")
        deployed = self.promote_model(
            project_id,
            decision_id,
            environment,
            previous["model_id"],
            key_id,
            f"rollback from {current.model_id} to historical version {previous['version']}",
        )
        return deployed, current.model_id

    def close(self) -> None:
        with self._lock:
            self._conn.close()
