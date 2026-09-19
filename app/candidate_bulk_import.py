from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from app.candidate_catalog import (
    CandidateCatalogItem,
    CandidateCatalogStore,
    CandidateCatalogUpsertRequest,
)
from app.candidate_catalog_ops import CandidateCatalogSnapshotCreate, CandidateCatalogSnapshotManager


ImportStatus = Literal[
    "queued",
    "running",
    "paused",
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
]
ImportFormat = Literal["csv", "jsonl"]
OnError = Literal["continue", "stop"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class CandidateBulkImportJob(BaseModel):
    job_id: str
    project_id: str
    catalog_id: str
    status: ImportStatus
    source_format: ImportFormat
    original_filename: str | None = None
    source_sha256: str
    source_bytes: int
    batch_size: int
    max_rows: int
    on_error: OnError
    snapshot_before_import: bool
    snapshot_id: str | None = None
    retry_of_job_id: str | None = None
    cursor_row: int = 0
    total_rows: int | None = None
    processed_rows: int = 0
    succeeded_rows: int = 0
    failed_rows: int = 0
    retryable_failed_rows: int = 0
    progress: float | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class CandidateBulkImportFailure(BaseModel):
    row_number: int
    item_id: str | None = None
    error: str
    retryable: bool
    raw_preview: str | None = None


class CandidateBulkImportRetryResponse(BaseModel):
    source_job_id: str
    retry_job: CandidateBulkImportJob


@dataclass
class _JobRow:
    job_id: str
    project_id: str
    catalog_id: str
    source_path: str
    source_format: str
    original_filename: str | None
    source_sha256: str
    source_bytes: int
    status: str
    batch_size: int
    max_rows: int
    on_error: str
    snapshot_before_import: bool
    snapshot_id: str | None
    retry_of_job_id: str | None
    cursor_row: int
    processed_rows: int
    succeeded_rows: int
    failed_rows: int
    total_rows: int | None


class CandidateBulkImportStore:
    """Durable control plane for project-scoped catalog import jobs.

    Source files live outside SQLite. Job state, failure rows and worker leases are
    durable so a process can resume work after a crash once the lease expires.
    """

    def __init__(self, path: str | None = None, upload_dir: str | None = None):
        self.path = path or os.getenv("RTDC_CANDIDATE_IMPORT_DB", "data/candidate_imports.sqlite3")
        self.upload_dir = Path(upload_dir or os.getenv("RTDC_CANDIDATE_IMPORT_DIR", "data/candidate_imports"))
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_import_jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_format TEXT NOT NULL,
                    original_filename TEXT,
                    source_sha256 TEXT NOT NULL,
                    source_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    batch_size INTEGER NOT NULL,
                    max_rows INTEGER NOT NULL,
                    on_error TEXT NOT NULL,
                    snapshot_before_import INTEGER NOT NULL,
                    snapshot_id TEXT,
                    retry_of_job_id TEXT,
                    cursor_row INTEGER NOT NULL DEFAULT 0,
                    total_rows INTEGER,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    succeeded_rows INTEGER NOT NULL DEFAULT 0,
                    failed_rows INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_import_failures (
                    job_id TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    item_id TEXT,
                    error TEXT NOT NULL,
                    payload_json TEXT,
                    raw_preview TEXT,
                    retryable INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, row_number),
                    FOREIGN KEY(job_id) REFERENCES candidate_import_jobs(job_id) ON DELETE CASCADE
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_import_queue "
                "ON candidate_import_jobs(status, created_at)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_import_project "
                "ON candidate_import_jobs(project_id, catalog_id, created_at DESC)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_import_failures "
                "ON candidate_import_failures(job_id, retryable, row_number)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def allocate_source_path(self, suffix: str) -> Path:
        safe_suffix = suffix.lower() if suffix.lower() in {".csv", ".jsonl", ".ndjson"} else ".upload"
        return self.upload_dir / ("src_" + uuid.uuid4().hex + safe_suffix)

    def create_job(
        self,
        *,
        project_id: str,
        catalog_id: str,
        source_path: str,
        source_format: ImportFormat,
        original_filename: str | None,
        source_sha256: str,
        source_bytes: int,
        batch_size: int,
        max_rows: int,
        on_error: OnError,
        snapshot_before_import: bool,
        retry_of_job_id: str | None = None,
    ) -> CandidateBulkImportJob:
        job_id = "imp_" + uuid.uuid4().hex[:24]
        now = _utc_now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO candidate_import_jobs (
                    job_id, project_id, catalog_id, source_path, source_format,
                    original_filename, source_sha256, source_bytes, status,
                    batch_size, max_rows, on_error, snapshot_before_import,
                    retry_of_job_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    project_id,
                    catalog_id,
                    source_path,
                    source_format,
                    original_filename,
                    source_sha256,
                    source_bytes,
                    batch_size,
                    max_rows,
                    on_error,
                    1 if snapshot_before_import else 0,
                    retry_of_job_id,
                    now,
                    now,
                ),
            )
        return self.get_job(project_id, job_id)

    def _to_model(self, row: sqlite3.Row) -> CandidateBulkImportJob:
        retryable = int(
            self._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_import_failures WHERE job_id=? AND retryable=1",
                (row["job_id"],),
            ).fetchone()["n"]
        )
        total = row["total_rows"]
        progress = None
        if total is not None and int(total) > 0:
            progress = min(1.0, int(row["processed_rows"]) / int(total))
        elif row["status"] in {"completed", "completed_with_errors"}:
            progress = 1.0
        return CandidateBulkImportJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            catalog_id=row["catalog_id"],
            status=row["status"],
            source_format=row["source_format"],
            original_filename=row["original_filename"],
            source_sha256=row["source_sha256"],
            source_bytes=int(row["source_bytes"]),
            batch_size=int(row["batch_size"]),
            max_rows=int(row["max_rows"]),
            on_error=row["on_error"],
            snapshot_before_import=bool(row["snapshot_before_import"]),
            snapshot_id=row["snapshot_id"],
            retry_of_job_id=row["retry_of_job_id"],
            cursor_row=int(row["cursor_row"]),
            total_rows=int(total) if total is not None else None,
            processed_rows=int(row["processed_rows"]),
            succeeded_rows=int(row["succeeded_rows"]),
            failed_rows=int(row["failed_rows"]),
            retryable_failed_rows=retryable,
            progress=progress,
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def get_job(self, project_id: str, job_id: str) -> CandidateBulkImportJob:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM candidate_import_jobs WHERE project_id=? AND job_id=?",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            return self._to_model(row)

    def get_job_row(self, job_id: str) -> _JobRow:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM candidate_import_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            return _JobRow(
                job_id=row["job_id"],
                project_id=row["project_id"],
                catalog_id=row["catalog_id"],
                source_path=row["source_path"],
                source_format=row["source_format"],
                original_filename=row["original_filename"],
                source_sha256=row["source_sha256"],
                source_bytes=int(row["source_bytes"]),
                status=row["status"],
                batch_size=int(row["batch_size"]),
                max_rows=int(row["max_rows"]),
                on_error=row["on_error"],
                snapshot_before_import=bool(row["snapshot_before_import"]),
                snapshot_id=row["snapshot_id"],
                retry_of_job_id=row["retry_of_job_id"],
                cursor_row=int(row["cursor_row"]),
                processed_rows=int(row["processed_rows"]),
                succeeded_rows=int(row["succeeded_rows"]),
                failed_rows=int(row["failed_rows"]),
                total_rows=int(row["total_rows"]) if row["total_rows"] is not None else None,
            )

    def list_jobs(self, project_id: str, catalog_id: str | None, limit: int = 100) -> list[CandidateBulkImportJob]:
        with self._lock:
            if catalog_id:
                rows = self._db.execute(
                    "SELECT * FROM candidate_import_jobs WHERE project_id=? AND catalog_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, catalog_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM candidate_import_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            return [self._to_model(row) for row in rows]

    def list_failures(self, project_id: str, job_id: str, limit: int = 100, offset: int = 0) -> list[CandidateBulkImportFailure]:
        self.get_job(project_id, job_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT row_number, item_id, error, retryable, raw_preview "
                "FROM candidate_import_failures WHERE job_id=? ORDER BY row_number LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
            return [
                CandidateBulkImportFailure(
                    row_number=int(row["row_number"]),
                    item_id=row["item_id"],
                    error=row["error"],
                    retryable=bool(row["retryable"]),
                    raw_preview=row["raw_preview"],
                )
                for row in rows
            ]

    def claim_next(self, worker_id: str, lease_seconds: int = 120) -> str | None:
        now = _utc_now()
        lease = _future(lease_seconds)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """SELECT j.job_id FROM candidate_import_jobs j
                    WHERE (
                        j.status='queued' OR
                        (j.status='running' AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at < ?)
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM candidate_import_jobs active
                        WHERE active.project_id=j.project_id AND active.catalog_id=j.catalog_id
                        AND active.status='running'
                        AND active.job_id<>j.job_id
                        AND active.lease_expires_at IS NOT NULL AND active.lease_expires_at >= ?
                    )
                    ORDER BY j.created_at ASC LIMIT 1""",
                    (now, now),
                ).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                job_id = row["job_id"]
                self._db.execute(
                    "UPDATE candidate_import_jobs SET status='running', worker_id=?, lease_expires_at=?, "
                    "started_at=COALESCE(started_at, ?), updated_at=? WHERE job_id=?",
                    (worker_id, lease, now, now, job_id),
                )
                self._db.commit()
                return job_id
            except Exception:
                self._db.rollback()
                raise

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 120) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate_import_jobs SET lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND worker_id=? AND status='running'",
                (_future(lease_seconds), _utc_now(), job_id, worker_id),
            )

    def current_status(self, job_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT status FROM candidate_import_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            return str(row["status"])

    def set_snapshot(self, job_id: str, snapshot_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate_import_jobs SET snapshot_id=?, updated_at=? WHERE job_id=?",
                (snapshot_id, _utc_now(), job_id),
            )

    def checkpoint(
        self,
        job_id: str,
        *,
        cursor_row: int,
        processed_rows: int,
        succeeded_rows: int,
        failed_rows: int,
        worker_id: str,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate_import_jobs SET cursor_row=?, processed_rows=?, succeeded_rows=?, "
                "failed_rows=?, lease_expires_at=?, updated_at=? WHERE job_id=? AND worker_id=?",
                (
                    cursor_row,
                    processed_rows,
                    succeeded_rows,
                    failed_rows,
                    _future(120),
                    _utc_now(),
                    job_id,
                    worker_id,
                ),
            )

    def record_failure(
        self,
        job_id: str,
        row_number: int,
        *,
        item_id: str | None,
        error: str,
        payload_json: str | None,
        raw_preview: str | None,
        retryable: bool,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO candidate_import_failures
                (job_id, row_number, item_id, error, payload_json, raw_preview, retryable, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, row_number) DO UPDATE SET
                    item_id=excluded.item_id, error=excluded.error, payload_json=excluded.payload_json,
                    raw_preview=excluded.raw_preview, retryable=excluded.retryable, created_at=excluded.created_at""",
                (
                    job_id,
                    row_number,
                    item_id,
                    error[:4000],
                    payload_json,
                    raw_preview[:2000] if raw_preview else None,
                    1 if retryable else 0,
                    _utc_now(),
                ),
            )

    def finish(self, job_id: str, *, total_rows: int, failed_rows: int, error_message: str | None = None) -> None:
        now = _utc_now()
        status = "completed" if failed_rows == 0 else "completed_with_errors"
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate_import_jobs SET status=?, total_rows=?, error_message=?, "
                "lease_expires_at=NULL, worker_id=NULL, finished_at=?, updated_at=? WHERE job_id=?",
                (status, total_rows, error_message, now, now, job_id),
            )

    def fail(self, job_id: str, message: str, total_rows: int | None = None) -> None:
        now = _utc_now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate_import_jobs SET status='failed', total_rows=COALESCE(?, total_rows), "
                "error_message=?, lease_expires_at=NULL, worker_id=NULL, finished_at=?, updated_at=? WHERE job_id=?",
                (total_rows, message[:4000], now, now, job_id),
            )

    def pause(self, project_id: str, job_id: str) -> CandidateBulkImportJob:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM candidate_import_jobs WHERE project_id=? AND job_id=?",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            if row["status"] not in {"queued", "running"}:
                raise ValueError(f"job cannot be paused from status={row['status']}")
            self._db.execute(
                "UPDATE candidate_import_jobs SET status='paused', lease_expires_at=NULL, worker_id=NULL, updated_at=? WHERE job_id=?",
                (_utc_now(), job_id),
            )
        return self.get_job(project_id, job_id)

    def resume(self, project_id: str, job_id: str) -> CandidateBulkImportJob:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM candidate_import_jobs WHERE project_id=? AND job_id=?",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            if row["status"] != "paused":
                raise ValueError(f"job cannot be resumed from status={row['status']}")
            self._db.execute(
                "UPDATE candidate_import_jobs SET status='queued', updated_at=? WHERE job_id=?",
                (_utc_now(), job_id),
            )
        return self.get_job(project_id, job_id)

    def cancel(self, project_id: str, job_id: str) -> CandidateBulkImportJob:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM candidate_import_jobs WHERE project_id=? AND job_id=?",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("candidate import job not found")
            if row["status"] not in {"queued", "running", "paused"}:
                raise ValueError(f"job cannot be cancelled from status={row['status']}")
            now = _utc_now()
            self._db.execute(
                "UPDATE candidate_import_jobs SET status='cancelled', lease_expires_at=NULL, worker_id=NULL, "
                "finished_at=?, updated_at=? WHERE job_id=?",
                (now, now, job_id),
            )
        return self.get_job(project_id, job_id)

    def create_retry_job(self, project_id: str, job_id: str) -> CandidateBulkImportJob:
        parent = self.get_job(project_id, job_id)
        if parent.status not in {"completed_with_errors", "failed"}:
            raise ValueError("failed rows can only be retried from a failed or completed_with_errors job")
        if parent.retryable_failed_rows <= 0:
            raise ValueError("job has no retryable failed rows")
        with self._lock:
            row = self._db.execute(
                "SELECT source_path FROM candidate_import_jobs WHERE project_id=? AND job_id=?",
                (project_id, job_id),
            ).fetchone()
        return self.create_job(
            project_id=parent.project_id,
            catalog_id=parent.catalog_id,
            source_path=row["source_path"],
            source_format=parent.source_format,
            original_filename=parent.original_filename,
            source_sha256=parent.source_sha256,
            source_bytes=parent.source_bytes,
            batch_size=parent.batch_size,
            max_rows=parent.retryable_failed_rows,
            on_error=parent.on_error,
            snapshot_before_import=False,
            retry_of_job_id=parent.job_id,
        )

    def iter_retry_payloads(self, parent_job_id: str) -> Iterable[tuple[int, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT row_number, payload_json FROM candidate_import_failures "
                "WHERE job_id=? AND retryable=1 AND payload_json IS NOT NULL ORDER BY row_number",
                (parent_job_id,),
            ).fetchall()
            return [(int(row["row_number"]), row["payload_json"]) for row in rows]


class CandidateBulkImportWorker:
    def __init__(
        self,
        jobs: CandidateBulkImportStore,
        catalog_store: CandidateCatalogStore,
        snapshots: CandidateCatalogSnapshotManager,
    ):
        self.jobs = jobs
        self.catalog_store = catalog_store
        self.snapshots = snapshots
        self.worker_id = "worker_" + uuid.uuid4().hex[:16]
        self.poll_seconds = max(0.1, float(os.getenv("RTDC_CANDIDATE_IMPORT_POLL_SECONDS", "1")))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run_loop(), name="candidate-bulk-import-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            job_id = await asyncio.to_thread(self.jobs.claim_next, self.worker_id)
            if job_id is None:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            await asyncio.to_thread(self.process_job, job_id)

    @staticmethod
    def _metadata_from_csv(row: dict[str, str | None]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        raw = row.get("metadata_json") or row.get("metadata")
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("metadata_json must contain a JSON object")
            metadata.update(parsed)
        for key, value in row.items():
            if key and key.startswith("meta_") and value not in {None, ""}:
                metadata[key[5:]] = value
        return metadata

    def _iter_source_rows(self, job: _JobRow):
        path = Path(job.source_path)
        if not path.exists():
            raise FileNotFoundError("bulk import source file is missing")
        if job.source_format == "jsonl":
            with path.open("r", encoding="utf-8") as handle:
                logical = 0
                for physical, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    logical += 1
                    try:
                        raw = json.loads(stripped)
                        if not isinstance(raw, dict):
                            raise ValueError("JSONL row must be an object")
                        item = CandidateCatalogItem.model_validate(raw)
                        yield logical, item, None, stripped[:2000]
                    except Exception as exc:
                        yield logical, None, f"line {physical}: {type(exc).__name__}: {exc}", stripped[:2000]
            return

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "id" not in reader.fieldnames or "text" not in reader.fieldnames:
                raise ValueError("CSV requires id and text columns")
            for logical, row in enumerate(reader, start=1):
                preview = json.dumps(row, ensure_ascii=False)[:2000]
                try:
                    item = CandidateCatalogItem(
                        id=(row.get("id") or "").strip(),
                        text=row.get("text") or "",
                        metadata=self._metadata_from_csv(row),
                    )
                    yield logical, item, None, preview
                except Exception as exc:
                    yield logical, None, f"CSV row {logical}: {type(exc).__name__}: {exc}", preview

    def _flush_batch(
        self,
        job: _JobRow,
        batch: list[tuple[int, CandidateCatalogItem, str | None]],
        succeeded: int,
        failed: int,
    ) -> tuple[int, int, bool]:
        if not batch:
            return succeeded, failed, False
        items = [item for _, item, _ in batch]
        try:
            self.catalog_store.upsert_items(
                job.project_id,
                job.catalog_id,
                CandidateCatalogUpsertRequest(items=items, max_total_chars=50_000_000),
            )
            return succeeded + len(items), failed, False
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            for row_number, item, preview in batch:
                self.jobs.record_failure(
                    job.job_id,
                    row_number,
                    item_id=item.id,
                    error=message,
                    payload_json=item.model_dump_json(),
                    raw_preview=preview,
                    retryable=True,
                )
            return succeeded, failed + len(items), job.on_error == "stop"

    def _process_source(self, job: _JobRow) -> None:
        if job.snapshot_before_import and job.snapshot_id is None:
            snapshot = self.snapshots.create_snapshot(
                job.project_id,
                job.catalog_id,
                CandidateCatalogSnapshotCreate(note=f"automatic pre-import snapshot for {job.job_id}"),
            )
            self.jobs.set_snapshot(job.job_id, snapshot.snapshot_id)
            job.snapshot_id = snapshot.snapshot_id

        cursor = job.cursor_row
        processed = job.processed_rows
        succeeded = job.succeeded_rows
        failed = job.failed_rows
        seen = cursor
        batch: list[tuple[int, CandidateCatalogItem, str | None]] = []

        try:
            for row_number, item, parse_error, preview in self._iter_source_rows(job):
                seen = row_number
                if row_number <= cursor:
                    continue
                if row_number > job.max_rows:
                    raise ValueError(f"import exceeds max_rows={job.max_rows}")

                if parse_error is not None or item is None:
                    failed += 1
                    processed += 1
                    self.jobs.record_failure(
                        job.job_id,
                        row_number,
                        item_id=None,
                        error=parse_error or "invalid row",
                        payload_json=None,
                        raw_preview=preview,
                        retryable=False,
                    )
                    self.jobs.checkpoint(
                        job.job_id,
                        cursor_row=row_number,
                        processed_rows=processed,
                        succeeded_rows=succeeded,
                        failed_rows=failed,
                        worker_id=self.worker_id,
                    )
                    if job.on_error == "stop":
                        self.jobs.fail(job.job_id, parse_error or "invalid row", total_rows=row_number)
                        return
                    continue

                batch.append((row_number, item, preview))
                if len(batch) < job.batch_size:
                    continue

                if self.jobs.current_status(job.job_id) == "cancelled":
                    return
                succeeded, failed, should_stop = self._flush_batch(job, batch, succeeded, failed)
                processed += len(batch)
                cursor = batch[-1][0]
                batch.clear()
                self.jobs.checkpoint(
                    job.job_id,
                    cursor_row=cursor,
                    processed_rows=processed,
                    succeeded_rows=succeeded,
                    failed_rows=failed,
                    worker_id=self.worker_id,
                )
                if should_stop:
                    self.jobs.fail(job.job_id, "catalog batch failed and on_error=stop", total_rows=cursor)
                    return
                status = self.jobs.current_status(job.job_id)
                if status in {"paused", "cancelled"}:
                    return

            if batch:
                if self.jobs.current_status(job.job_id) == "cancelled":
                    return
                succeeded, failed, should_stop = self._flush_batch(job, batch, succeeded, failed)
                processed += len(batch)
                cursor = batch[-1][0]
                batch.clear()
                self.jobs.checkpoint(
                    job.job_id,
                    cursor_row=cursor,
                    processed_rows=processed,
                    succeeded_rows=succeeded,
                    failed_rows=failed,
                    worker_id=self.worker_id,
                )
                if should_stop:
                    self.jobs.fail(job.job_id, "catalog batch failed and on_error=stop", total_rows=cursor)
                    return

            if self.jobs.current_status(job.job_id) == "running":
                self.jobs.finish(job.job_id, total_rows=seen, failed_rows=failed)
        except Exception as exc:
            self.jobs.fail(job.job_id, f"{type(exc).__name__}: {exc}", total_rows=seen or None)

    def _process_retry(self, job: _JobRow) -> None:
        assert job.retry_of_job_id is not None
        rows = list(self.jobs.iter_retry_payloads(job.retry_of_job_id))
        processed = 0
        succeeded = 0
        failed = 0
        batch: list[tuple[int, CandidateCatalogItem, str | None]] = []
        try:
            for index, (source_row, payload_json) in enumerate(rows, start=1):
                if self.jobs.current_status(job.job_id) in {"paused", "cancelled"}:
                    return
                item = CandidateCatalogItem.model_validate_json(payload_json)
                batch.append((source_row, item, payload_json[:2000]))
                if len(batch) < job.batch_size:
                    continue
                succeeded, failed, should_stop = self._flush_batch(job, batch, succeeded, failed)
                processed += len(batch)
                batch.clear()
                self.jobs.checkpoint(
                    job.job_id,
                    cursor_row=index,
                    processed_rows=processed,
                    succeeded_rows=succeeded,
                    failed_rows=failed,
                    worker_id=self.worker_id,
                )
                if should_stop:
                    self.jobs.fail(job.job_id, "retry batch failed and on_error=stop", total_rows=len(rows))
                    return
            if batch:
                succeeded, failed, should_stop = self._flush_batch(job, batch, succeeded, failed)
                processed += len(batch)
                self.jobs.checkpoint(
                    job.job_id,
                    cursor_row=len(rows),
                    processed_rows=processed,
                    succeeded_rows=succeeded,
                    failed_rows=failed,
                    worker_id=self.worker_id,
                )
                if should_stop:
                    self.jobs.fail(job.job_id, "retry batch failed and on_error=stop", total_rows=len(rows))
                    return
            if self.jobs.current_status(job.job_id) == "running":
                self.jobs.finish(job.job_id, total_rows=len(rows), failed_rows=failed)
        except Exception as exc:
            self.jobs.fail(job.job_id, f"{type(exc).__name__}: {exc}", total_rows=len(rows))

    def process_job(self, job_id: str) -> None:
        job = self.jobs.get_job_row(job_id)
        self.catalog_store.get_catalog(job.project_id, job.catalog_id)
        self.jobs.heartbeat(job_id, self.worker_id)
        if job.retry_of_job_id:
            self._process_retry(job)
        else:
            self._process_source(job)


class CandidateBulkImportServices:
    def __init__(self, jobs: CandidateBulkImportStore, catalog_store: CandidateCatalogStore, worker: CandidateBulkImportWorker):
        self.jobs = jobs
        self.catalog_store = catalog_store
        self.worker = worker

    async def close(self) -> None:
        await self.worker.stop()
        self.catalog_store.close()
        self.jobs.close()
