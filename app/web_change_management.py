from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

from app.web_intelligence import WebsiteAuditEngine, WebsiteAuditRequest, WebsiteAuditResponse, _assert_public_url


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_TAG_SPLIT = re.compile(r"(<[^>]+>)")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _url_key(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), path, "", ""))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _short_snippet(value: str, needle: str, radius: int = 180) -> str:
    idx = value.find(needle)
    if idx < 0:
        return value[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(value), idx + len(needle) + radius)
    return value[start:end]


def insert_internal_link_once(source_html: str, anchor_text: str, target_url: str) -> tuple[str, str, str] | None:
    """Insert one link only in an ordinary text node.

    The function deliberately avoids existing anchors, scripts, styles, code and pre blocks.
    It preserves the original markup outside the replaced text node and refuses to guess when
    the anchor text is not present as one contiguous text node.
    """
    anchor = anchor_text.strip()
    if not anchor:
        return None
    target_escaped = html.escape(target_url, quote=True)
    href_patterns = (f'href="{target_escaped}"', f"href='{target_escaped}'")
    lowered = source_html.lower()
    if any(pattern.lower() in lowered for pattern in href_patterns):
        return None

    parts = _TAG_SPLIT.split(source_html)
    skip_depth = 0
    anchor_depth = 0
    skip_tags = {"script", "style", "code", "pre", "textarea"}
    for index, part in enumerate(parts):
        if part.startswith("<") and part.endswith(">"):
            match = re.match(r"<\s*(/?)\s*([a-zA-Z0-9]+)", part)
            if not match:
                continue
            closing, tag = match.group(1), match.group(2).lower()
            self_closing = part.rstrip().endswith("/>")
            if tag == "a":
                if closing:
                    anchor_depth = max(0, anchor_depth - 1)
                elif not self_closing:
                    anchor_depth += 1
            if tag in skip_tags:
                if closing:
                    skip_depth = max(0, skip_depth - 1)
                elif not self_closing:
                    skip_depth += 1
            continue
        if skip_depth or anchor_depth:
            continue
        pos = part.find(anchor)
        if pos < 0:
            continue
        replacement = (
            part[:pos]
            + f'<a href="{target_escaped}">{part[pos:pos + len(anchor)]}</a>'
            + part[pos + len(anchor):]
        )
        before = _short_snippet(part, anchor)
        after = _short_snippet(replacement, target_escaped)
        parts[index] = replacement
        return "".join(parts), before, after
    return None


class WebsiteAuditOptions(BaseModel):
    max_pages: int = Field(default=250, ge=1, le=1000)
    concurrency: int = Field(default=8, ge=1, le=32)
    request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    max_page_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    respect_robots_txt: bool = True
    include_query_strings: bool = False
    suggestions_per_page: int = Field(default=5, ge=1, le=25)
    min_semantic_score: float = Field(default=0.18, ge=0.0, le=1.0)

    def build(self, start_url: str) -> WebsiteAuditRequest:
        return WebsiteAuditRequest(start_url=start_url, **self.model_dump())


class WordPressConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    site_url: str = Field(min_length=8, max_length=2048)
    username_env: str = Field(min_length=2, max_length=128)
    application_password_env: str = Field(min_length=2, max_length=128)
    enabled: bool = True

    def validate_env_names(self) -> None:
        if not _ENV_NAME.fullmatch(self.username_env):
            raise ValueError("username_env must be an uppercase environment variable name")
        if not _ENV_NAME.fullmatch(self.application_password_env):
            raise ValueError("application_password_env must be an uppercase environment variable name")


class WordPressConnectorSummary(BaseModel):
    id: str
    project_id: str
    name: str
    site_url: str
    username_env: str
    application_password_env: str
    enabled: bool
    created_at: str
    updated_at: str
    last_tested_at: str | None = None
    last_test_status: str | None = None


class ConnectorTestResult(BaseModel):
    connector_id: str
    ok: bool
    site_url: str
    wordpress_user: str | None = None
    detail: str


ProposalStatus = Literal["pending", "approved", "rejected", "applied", "stale", "failed"]


class LinkChangeProposal(BaseModel):
    id: str
    project_id: str
    connector_id: str
    audit_run_id: str
    source_url: str
    target_url: str
    score: float
    anchor_text: str
    source_object_type: str | None = None
    source_object_id: int | None = None
    source_revision_hash: str | None = None
    auto_applicable: bool = False
    status: ProposalStatus
    preview_before: str | None = None
    preview_after: str | None = None
    reason: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    applied_at: str | None = None


class StageAuditRequest(BaseModel):
    connector_id: str = Field(min_length=4, max_length=80)
    audit: WebsiteAuditOptions = Field(default_factory=WebsiteAuditOptions)


class AuditRunSummary(BaseModel):
    id: str
    project_id: str
    connector_id: str
    trigger: Literal["manual", "schedule"]
    status: Literal["running", "completed", "failed"]
    pages_crawled: int = 0
    proposals_created: int = 0
    started_at: str
    completed_at: str | None = None
    error: str | None = None


class StagedAuditResponse(BaseModel):
    run: AuditRunSummary
    audit: WebsiteAuditResponse
    proposals: list[LinkChangeProposal]


class ScheduleCreate(BaseModel):
    connector_id: str = Field(min_length=4, max_length=80)
    interval_minutes: int = Field(default=1440, ge=15, le=43_200)
    enabled: bool = True
    audit: WebsiteAuditOptions = Field(default_factory=WebsiteAuditOptions)


class ScheduleSummary(BaseModel):
    id: str
    project_id: str
    connector_id: str
    interval_minutes: int
    enabled: bool
    audit: WebsiteAuditOptions
    next_run_at: str
    last_run_at: str | None = None
    last_status: str | None = None
    created_at: str
    updated_at: str


class WebChangeStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_WEB_CHANGE_DB", "data/web_changes.sqlite3")
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_connectors (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    site_url TEXT NOT NULL,
                    username_env TEXT NOT NULL,
                    application_password_env TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_tested_at TEXT,
                    last_test_status TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_web_connectors_project ON web_connectors(project_id, created_at);

                CREATE TABLE IF NOT EXISTS web_audit_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pages_crawled INTEGER NOT NULL DEFAULT 0,
                    proposals_created INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_web_runs_project ON web_audit_runs(project_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS web_change_proposals (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    audit_run_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    score REAL NOT NULL,
                    anchor_text TEXT NOT NULL,
                    source_object_type TEXT,
                    source_object_id INTEGER,
                    source_revision_hash TEXT,
                    auto_applicable INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    preview_before TEXT,
                    preview_after TEXT,
                    reason TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_web_proposals_project_status ON web_change_proposals(project_id, status, created_at DESC);

                CREATE TABLE IF NOT EXISTS web_audit_schedules (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    audit_json TEXT NOT NULL,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_web_schedules_due ON web_audit_schedules(enabled, next_run_at);
                """
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _connector(row: sqlite3.Row) -> WordPressConnectorSummary:
        return WordPressConnectorSummary(
            id=row["id"], project_id=row["project_id"], name=row["name"], site_url=row["site_url"],
            username_env=row["username_env"], application_password_env=row["application_password_env"],
            enabled=bool(row["enabled"]), created_at=row["created_at"], updated_at=row["updated_at"],
            last_tested_at=row["last_tested_at"], last_test_status=row["last_test_status"],
        )

    @staticmethod
    def _proposal(row: sqlite3.Row) -> LinkChangeProposal:
        return LinkChangeProposal(
            id=row["id"], project_id=row["project_id"], connector_id=row["connector_id"], audit_run_id=row["audit_run_id"],
            source_url=row["source_url"], target_url=row["target_url"], score=row["score"], anchor_text=row["anchor_text"],
            source_object_type=row["source_object_type"], source_object_id=row["source_object_id"], source_revision_hash=row["source_revision_hash"],
            auto_applicable=bool(row["auto_applicable"]), status=row["status"], preview_before=row["preview_before"],
            preview_after=row["preview_after"], reason=row["reason"], last_error=row["last_error"],
            created_at=row["created_at"], updated_at=row["updated_at"], applied_at=row["applied_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> AuditRunSummary:
        return AuditRunSummary(**dict(row))

    @staticmethod
    def _schedule(row: sqlite3.Row) -> ScheduleSummary:
        return ScheduleSummary(
            id=row["id"], project_id=row["project_id"], connector_id=row["connector_id"],
            interval_minutes=row["interval_minutes"], enabled=bool(row["enabled"]),
            audit=WebsiteAuditOptions.model_validate(json.loads(row["audit_json"])), next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"], last_status=row["last_status"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def create_connector(self, project_id: str, payload: WordPressConnectorCreate) -> WordPressConnectorSummary:
        now = _iso()
        connector_id = "wpc_" + uuid.uuid4().hex[:20]
        with self._lock:
            self._db.execute(
                "INSERT INTO web_connectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (connector_id, project_id, payload.name, payload.site_url.rstrip("/"), payload.username_env,
                 payload.application_password_env, int(payload.enabled), now, now),
            )
            self._db.commit()
        return self.get_connector(project_id, connector_id)

    def get_connector(self, project_id: str, connector_id: str) -> WordPressConnectorSummary:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM web_connectors WHERE id=? AND project_id=?", (connector_id, project_id)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("connector not found")
        return self._connector(row)

    def list_connectors(self, project_id: str) -> list[WordPressConnectorSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_connectors WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [self._connector(row) for row in rows]

    def record_connector_test(self, project_id: str, connector_id: str, detail: str) -> None:
        now = _iso()
        with self._lock:
            result = self._db.execute(
                "UPDATE web_connectors SET last_tested_at=?, last_test_status=?, updated_at=? WHERE id=? AND project_id=?",
                (now, detail[:500], now, connector_id, project_id),
            )
            self._db.commit()
        if result.rowcount != 1:
            raise FileNotFoundError("connector not found")

    def create_run(self, project_id: str, connector_id: str, trigger: str) -> AuditRunSummary:
        run_id = "war_" + uuid.uuid4().hex[:20]
        now = _iso()
        with self._lock:
            self._db.execute(
                "INSERT INTO web_audit_runs (id, project_id, connector_id, trigger, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, project_id, connector_id, trigger, now),
            )
            self._db.commit()
        return self.get_run(project_id, run_id)

    def get_run(self, project_id: str, run_id: str) -> AuditRunSummary:
        with self._lock:
            row = self._db.execute("SELECT * FROM web_audit_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
        if row is None:
            raise FileNotFoundError("audit run not found")
        return self._run(row)

    def finish_run(self, project_id: str, run_id: str, status: str, pages_crawled: int, proposals_created: int, error: str | None = None) -> AuditRunSummary:
        with self._lock:
            self._db.execute(
                "UPDATE web_audit_runs SET status=?, pages_crawled=?, proposals_created=?, completed_at=?, error=? WHERE id=? AND project_id=?",
                (status, pages_crawled, proposals_created, _iso(), error[:2000] if error else None, run_id, project_id),
            )
            self._db.commit()
        return self.get_run(project_id, run_id)

    def list_runs(self, project_id: str, limit: int = 100) -> list[AuditRunSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_audit_runs WHERE project_id=? ORDER BY started_at DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        return [self._run(row) for row in rows]

    def create_proposal(self, proposal: LinkChangeProposal) -> tuple[LinkChangeProposal, bool]:
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM web_change_proposals WHERE project_id=? AND connector_id=? AND source_url=? AND target_url=? AND status IN ('pending','approved') ORDER BY created_at DESC LIMIT 1",
                (proposal.project_id, proposal.connector_id, proposal.source_url, proposal.target_url),
            ).fetchone()
            if existing is not None:
                return self._proposal(existing), False
            self._db.execute(
                """INSERT INTO web_change_proposals
                (id, project_id, connector_id, audit_run_id, source_url, target_url, score, anchor_text,
                 source_object_type, source_object_id, source_revision_hash, auto_applicable, status,
                 preview_before, preview_after, reason, last_error, created_at, updated_at, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal.id, proposal.project_id, proposal.connector_id, proposal.audit_run_id, proposal.source_url,
                 proposal.target_url, proposal.score, proposal.anchor_text, proposal.source_object_type,
                 proposal.source_object_id, proposal.source_revision_hash, int(proposal.auto_applicable), proposal.status,
                 proposal.preview_before, proposal.preview_after, proposal.reason, proposal.last_error,
                 proposal.created_at, proposal.updated_at, proposal.applied_at),
            )
            self._db.commit()
        return proposal, True

    def get_proposal(self, project_id: str, proposal_id: str) -> LinkChangeProposal:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM web_change_proposals WHERE id=? AND project_id=?", (proposal_id, project_id)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("proposal not found")
        return self._proposal(row)

    def list_proposals(self, project_id: str, status: str | None = None, limit: int = 200) -> list[LinkChangeProposal]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM web_change_proposals WHERE project_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                    (project_id, status, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM web_change_proposals WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
        return [self._proposal(row) for row in rows]

    def update_proposal_status(self, project_id: str, proposal_id: str, status: ProposalStatus, error: str | None = None) -> LinkChangeProposal:
        now = _iso()
        applied_at = now if status == "applied" else None
        with self._lock:
            result = self._db.execute(
                "UPDATE web_change_proposals SET status=?, last_error=?, updated_at=?, applied_at=COALESCE(?, applied_at) WHERE id=? AND project_id=?",
                (status, error[:2000] if error else None, now, applied_at, proposal_id, project_id),
            )
            self._db.commit()
        if result.rowcount != 1:
            raise FileNotFoundError("proposal not found")
        return self.get_proposal(project_id, proposal_id)

    def create_schedule(self, project_id: str, payload: ScheduleCreate) -> ScheduleSummary:
        schedule_id = "was_" + uuid.uuid4().hex[:20]
        now = _utc_now()
        next_run = now + timedelta(minutes=payload.interval_minutes)
        with self._lock:
            self._db.execute(
                "INSERT INTO web_audit_schedules VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (schedule_id, project_id, payload.connector_id, payload.interval_minutes, int(payload.enabled),
                 json.dumps(payload.audit.model_dump(), ensure_ascii=False, sort_keys=True), next_run.isoformat(), now.isoformat(), now.isoformat()),
            )
            self._db.commit()
        return self.get_schedule(project_id, schedule_id)

    def get_schedule(self, project_id: str, schedule_id: str) -> ScheduleSummary:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM web_audit_schedules WHERE id=? AND project_id=?", (schedule_id, project_id)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("schedule not found")
        return self._schedule(row)

    def list_schedules(self, project_id: str) -> list[ScheduleSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM web_audit_schedules WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [self._schedule(row) for row in rows]

    def delete_schedule(self, project_id: str, schedule_id: str) -> bool:
        with self._lock:
            result = self._db.execute("DELETE FROM web_audit_schedules WHERE id=? AND project_id=?", (schedule_id, project_id))
            self._db.commit()
        return result.rowcount == 1

    def claim_due_schedules(self, limit: int = 10) -> list[ScheduleSummary]:
        now = _utc_now()
        claimed: list[ScheduleSummary] = []
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            rows = self._db.execute(
                "SELECT * FROM web_audit_schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                next_run = now + timedelta(minutes=row["interval_minutes"])
                self._db.execute(
                    "UPDATE web_audit_schedules SET next_run_at=?, last_run_at=?, last_status='running', updated_at=? WHERE id=?",
                    (next_run.isoformat(), now.isoformat(), now.isoformat(), row["id"]),
                )
            self._db.commit()
            for row in rows:
                refreshed = self._db.execute("SELECT * FROM web_audit_schedules WHERE id=?", (row["id"],)).fetchone()
                claimed.append(self._schedule(refreshed))
        return claimed

    def finish_schedule(self, schedule_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE web_audit_schedules SET last_status=?, updated_at=? WHERE id=?",
                (status[:200], _iso(), schedule_id),
            )
            self._db.commit()


@dataclass
class WordPressObject:
    collection: str
    object_id: int
    link: str
    modified_gmt: str | None
    raw_content: str | None


class WordPressClient:
    def __init__(self, connector: WordPressConnectorSummary):
        self.connector = connector
        self.username = os.getenv(connector.username_env, "")
        self.password = os.getenv(connector.application_password_env, "")
        if not self.username or not self.password:
            raise RuntimeError(
                f"WordPress credentials are missing; configure {connector.username_env} and {connector.application_password_env}"
            )
        self.origin = _url_key(connector.site_url)
        self.api_root = connector.site_url.rstrip("/") + "/wp-json/wp/v2/"

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = urljoin(self.api_root, path.lstrip("/"))
        await _assert_public_url(url)
        if (urlsplit(url).scheme.lower(), urlsplit(url).hostname, urlsplit(url).port or (443 if urlsplit(url).scheme == "https" else 80)) != (
            urlsplit(self.connector.site_url).scheme.lower(), urlsplit(self.connector.site_url).hostname,
            urlsplit(self.connector.site_url).port or (443 if urlsplit(self.connector.site_url).scheme == "https" else 80),
        ):
            raise ValueError("WordPress API request escaped the configured origin")
        async with httpx.AsyncClient(auth=httpx.BasicAuth(self.username, self.password), trust_env=False, follow_redirects=False) as client:
            return await client.request(method, url, timeout=20.0, **kwargs)

    async def test_connection(self) -> ConnectorTestResult:
        response = await self._request("GET", "users/me", params={"context": "edit", "_fields": "id,name"})
        if response.status_code >= 400:
            return ConnectorTestResult(
                connector_id=self.connector.id, ok=False, site_url=self.connector.site_url,
                detail=f"WordPress REST API returned HTTP {response.status_code}",
            )
        payload = response.json()
        return ConnectorTestResult(
            connector_id=self.connector.id, ok=True, site_url=self.connector.site_url,
            wordpress_user=str(payload.get("name") or payload.get("id") or "authenticated user"), detail="authenticated",
        )

    async def discover_objects(self, wanted_urls: set[str]) -> dict[str, WordPressObject]:
        found: dict[str, WordPressObject] = {}
        max_items = int(os.getenv("RTDC_WP_DISCOVERY_MAX_ITEMS", "5000"))
        scanned = 0
        for collection in ("pages", "posts"):
            page = 1
            while scanned < max_items and len(found) < len(wanted_urls):
                response = await self._request(
                    "GET", collection,
                    params={"context": "edit", "per_page": 100, "page": page, "_fields": "id,link,modified_gmt,content"},
                )
                if response.status_code == 400 and page > 1:
                    break
                response.raise_for_status()
                items = response.json()
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    scanned += 1
                    content = item.get("content") or {}
                    raw = content.get("raw") if isinstance(content, dict) else None
                    obj = WordPressObject(
                        collection=collection, object_id=int(item["id"]), link=str(item.get("link") or ""),
                        modified_gmt=item.get("modified_gmt"), raw_content=raw if isinstance(raw, str) else None,
                    )
                    key = _url_key(obj.link)
                    if key in wanted_urls:
                        found[key] = obj
                    if scanned >= max_items:
                        break
                if len(items) < 100:
                    break
                page += 1
        return found

    async def get_object(self, collection: str, object_id: int) -> WordPressObject:
        if collection not in {"pages", "posts"}:
            raise ValueError("unsupported WordPress object collection")
        response = await self._request(
            "GET", f"{collection}/{object_id}", params={"context": "edit", "_fields": "id,link,modified_gmt,content"}
        )
        response.raise_for_status()
        item = response.json()
        content = item.get("content") or {}
        raw = content.get("raw") if isinstance(content, dict) else None
        return WordPressObject(
            collection=collection, object_id=int(item["id"]), link=str(item.get("link") or ""),
            modified_gmt=item.get("modified_gmt"), raw_content=raw if isinstance(raw, str) else None,
        )

    async def update_content(self, collection: str, object_id: int, content: str) -> None:
        response = await self._request("POST", f"{collection}/{object_id}", json={"content": content})
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("id", -1)) != object_id:
            raise RuntimeError("WordPress update returned an unexpected object id")


class WebChangeService:
    def __init__(self, audit_engine: WebsiteAuditEngine, store: WebChangeStore | None = None):
        self.audit_engine = audit_engine
        self.store = store or WebChangeStore()
        self._scheduler_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def create_connector(self, project_id: str, payload: WordPressConnectorCreate) -> WordPressConnectorSummary:
        payload.validate_env_names()
        await _assert_public_url(payload.site_url)
        return self.store.create_connector(project_id, payload)

    async def test_connector(self, project_id: str, connector_id: str) -> ConnectorTestResult:
        connector = self.store.get_connector(project_id, connector_id)
        try:
            result = await WordPressClient(connector).test_connection()
            self.store.record_connector_test(project_id, connector_id, "ok" if result.ok else result.detail)
            return result
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.store.record_connector_test(project_id, connector_id, detail)
            return ConnectorTestResult(connector_id=connector_id, ok=False, site_url=connector.site_url, detail=detail)

    async def stage_audit(self, project_id: str, payload: StageAuditRequest, trigger: Literal["manual", "schedule"] = "manual") -> StagedAuditResponse:
        connector = self.store.get_connector(project_id, payload.connector_id)
        if not connector.enabled:
            raise PermissionError("connector is disabled")
        run = self.store.create_run(project_id, connector.id, trigger)
        try:
            audit = await self.audit_engine.audit(payload.audit.build(connector.site_url))
            source_keys = {_url_key(item.source_url) for item in audit.internal_link_suggestions}
            objects: dict[str, WordPressObject] = {}
            discovery_error: str | None = None
            try:
                objects = await WordPressClient(connector).discover_objects(source_keys)
            except Exception as exc:
                discovery_error = f"WordPress discovery unavailable: {type(exc).__name__}: {exc}"

            max_proposals = int(os.getenv("RTDC_WEB_MAX_PROPOSALS_PER_AUDIT", "500"))
            created: list[LinkChangeProposal] = []
            for suggestion in audit.internal_link_suggestions[:max_proposals]:
                obj = objects.get(_url_key(suggestion.source_url))
                auto = False
                before = after = revision_hash = None
                reason = discovery_error
                object_type = None
                object_id = None
                if obj is not None:
                    object_type = obj.collection
                    object_id = obj.object_id
                    if obj.raw_content is None:
                        reason = "WordPress content.raw is unavailable for this object"
                    else:
                        revision_hash = _sha256_text(obj.raw_content)
                        patched = insert_internal_link_once(obj.raw_content, suggestion.suggested_anchor, suggestion.target_url)
                        if patched is not None:
                            _, before, after = patched
                            auto = True
                            reason = "exact unlinked anchor text found in WordPress raw content"
                        else:
                            reason = "no safe exact text-node insertion point was found; manual edit required"
                elif reason is None:
                    reason = "source URL could not be mapped to an editable WordPress page/post"

                now = _iso()
                proposal = LinkChangeProposal(
                    id="wcp_" + uuid.uuid4().hex[:20], project_id=project_id, connector_id=connector.id,
                    audit_run_id=run.id, source_url=suggestion.source_url, target_url=suggestion.target_url,
                    score=suggestion.score, anchor_text=suggestion.suggested_anchor, source_object_type=object_type,
                    source_object_id=object_id, source_revision_hash=revision_hash, auto_applicable=auto,
                    status="pending", preview_before=before, preview_after=after, reason=reason,
                    created_at=now, updated_at=now,
                )
                stored, was_created = self.store.create_proposal(proposal)
                if was_created:
                    created.append(stored)

            finished = self.store.finish_run(project_id, run.id, "completed", audit.pages_crawled, len(created))
            return StagedAuditResponse(run=finished, audit=audit, proposals=created)
        except Exception as exc:
            self.store.finish_run(project_id, run.id, "failed", 0, 0, f"{type(exc).__name__}: {exc}")
            raise

    def approve(self, project_id: str, proposal_id: str) -> LinkChangeProposal:
        proposal = self.store.get_proposal(project_id, proposal_id)
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal cannot be approved from status {proposal.status}")
        return self.store.update_proposal_status(project_id, proposal_id, "approved")

    def reject(self, project_id: str, proposal_id: str) -> LinkChangeProposal:
        proposal = self.store.get_proposal(project_id, proposal_id)
        if proposal.status == "applied":
            raise ValueError("an applied proposal cannot be rejected")
        return self.store.update_proposal_status(project_id, proposal_id, "rejected")

    async def apply(self, project_id: str, proposal_id: str) -> LinkChangeProposal:
        proposal = self.store.get_proposal(project_id, proposal_id)
        if proposal.status == "applied":
            return proposal
        if proposal.status != "approved":
            raise ValueError("proposal must be approved before apply")
        if not proposal.auto_applicable or proposal.source_object_type is None or proposal.source_object_id is None or proposal.source_revision_hash is None:
            raise ValueError("proposal is reviewable but not safe for automatic apply")
        connector = self.store.get_connector(project_id, proposal.connector_id)
        client = WordPressClient(connector)
        try:
            current = await client.get_object(proposal.source_object_type, proposal.source_object_id)
            if _url_key(current.link) != _url_key(proposal.source_url):
                return self.store.update_proposal_status(project_id, proposal_id, "stale", "WordPress object URL changed")
            if current.raw_content is None:
                return self.store.update_proposal_status(project_id, proposal_id, "stale", "WordPress raw content is unavailable")
            if _sha256_text(current.raw_content) != proposal.source_revision_hash:
                return self.store.update_proposal_status(project_id, proposal_id, "stale", "source content changed after review; run a new audit")
            patched = insert_internal_link_once(current.raw_content, proposal.anchor_text, proposal.target_url)
            if patched is None:
                return self.store.update_proposal_status(project_id, proposal_id, "stale", "safe insertion point no longer exists")
            updated_html, _, _ = patched
            await client.update_content(proposal.source_object_type, proposal.source_object_id, updated_html)
            return self.store.update_proposal_status(project_id, proposal_id, "applied")
        except httpx.HTTPStatusError as exc:
            return self.store.update_proposal_status(
                project_id, proposal_id, "failed", f"WordPress HTTP {exc.response.status_code}"
            )
        except Exception as exc:
            return self.store.update_proposal_status(project_id, proposal_id, "failed", f"{type(exc).__name__}: {exc}")

    async def approve_and_apply(self, project_id: str, proposal_id: str) -> LinkChangeProposal:
        self.approve(project_id, proposal_id)
        return await self.apply(project_id, proposal_id)

    async def start_scheduler(self) -> None:
        if os.getenv("RTDC_WEB_SCHEDULER_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            return
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._stopping.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="rtdc-web-audit-scheduler")

    async def stop_scheduler(self) -> None:
        self._stopping.set()
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        poll = max(5, int(os.getenv("RTDC_WEB_SCHEDULER_POLL_SECONDS", "60")))
        while not self._stopping.is_set():
            for schedule in self.store.claim_due_schedules(limit=10):
                try:
                    await self.stage_audit(
                        schedule.project_id,
                        StageAuditRequest(connector_id=schedule.connector_id, audit=schedule.audit),
                        trigger="schedule",
                    )
                    self.store.finish_schedule(schedule.id, "completed")
                except Exception as exc:
                    self.store.finish_schedule(schedule.id, f"failed: {type(exc).__name__}: {exc}")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
