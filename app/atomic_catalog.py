from __future__ import annotations

import asyncio
import csv
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from app.candidate_catalog import (
    CandidateCatalogItem,
    CandidateCatalogSearchItem,
    CandidateCatalogSearchRequest,
    CandidateCatalogSearchResponse,
    _StoredCandidate,
)
from app.operation_models import RankCandidate, RankRequest
from app.operations import OperationalDecisionEngine
from app.semantic_matrix import sparse_signature
from app.shared_object_store import SharedObjectStore, file_sha256


GenerationStatus = Literal["queued", "building", "ready", "active", "retired", "failed"]
SourceFormat = Literal["csv", "jsonl"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class AtomicCatalogGeneration(BaseModel):
    generation_id: str
    project_id: str
    catalog_id: str
    status: GenerationStatus
    source_uri: str
    source_sha256: str
    source_format: SourceFormat
    artifact_uri: str | None = None
    artifact_sha256: str | None = None
    item_count: int = 0
    feature_rows: int = 0
    database_bytes: int = 0
    build_seconds: float | None = None
    batch_size: int
    max_rows: int
    feature_dim: int
    ngram_min: int
    ngram_max: int
    auto_activate: bool
    created_at: str
    updated_at: str
    ready_at: str | None = None
    activated_at: str | None = None
    error_message: str | None = None


class AtomicCatalogActive(BaseModel):
    project_id: str
    catalog_id: str
    generation_id: str
    previous_generation_id: str | None = None
    revision: int
    updated_at: str


class AtomicCatalogActivation(BaseModel):
    active: AtomicCatalogActive
    previous_generation_id: str | None = None
    swap_latency_ms: float
    prewarmed: bool = True


class AtomicCatalogBuildResult(BaseModel):
    database_path: str
    item_count: int
    feature_rows: int
    database_bytes: int
    build_seconds: float


class AtomicCatalogControlStore:
    """Durable generation metadata and an atomic active-generation pointer.

    The reference implementation uses a short SQLite transaction for the pointer
    flip. Immutable index artifacts live in SharedObjectStore and are never
    modified in place after they become ready.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_ATOMIC_CATALOG_DB", "data/atomic_catalogs.sqlite3")
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
                """CREATE TABLE IF NOT EXISTS atomic_catalog_generations (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_format TEXT NOT NULL,
                    artifact_uri TEXT,
                    artifact_sha256 TEXT,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    feature_rows INTEGER NOT NULL DEFAULT 0,
                    database_bytes INTEGER NOT NULL DEFAULT 0,
                    build_seconds REAL,
                    batch_size INTEGER NOT NULL,
                    max_rows INTEGER NOT NULL,
                    feature_dim INTEGER NOT NULL,
                    ngram_min INTEGER NOT NULL,
                    ngram_max INTEGER NOT NULL,
                    auto_activate INTEGER NOT NULL,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ready_at TEXT,
                    activated_at TEXT,
                    error_message TEXT,
                    PRIMARY KEY(project_id, catalog_id, generation_id)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS atomic_catalog_active (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    previous_generation_id TEXT,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_id)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS atomic_catalog_deployments (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    from_generation_id TEXT,
                    to_generation_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_id, revision)
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atomic_generation_queue "
                "ON atomic_catalog_generations(status, created_at)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _generation(self, row: sqlite3.Row) -> AtomicCatalogGeneration:
        return AtomicCatalogGeneration(
            generation_id=row["generation_id"],
            project_id=row["project_id"],
            catalog_id=row["catalog_id"],
            status=row["status"],
            source_uri=row["source_uri"],
            source_sha256=row["source_sha256"],
            source_format=row["source_format"],
            artifact_uri=row["artifact_uri"],
            artifact_sha256=row["artifact_sha256"],
            item_count=int(row["item_count"]),
            feature_rows=int(row["feature_rows"]),
            database_bytes=int(row["database_bytes"]),
            build_seconds=float(row["build_seconds"]) if row["build_seconds"] is not None else None,
            batch_size=int(row["batch_size"]),
            max_rows=int(row["max_rows"]),
            feature_dim=int(row["feature_dim"]),
            ngram_min=int(row["ngram_min"]),
            ngram_max=int(row["ngram_max"]),
            auto_activate=bool(row["auto_activate"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ready_at=row["ready_at"],
            activated_at=row["activated_at"],
            error_message=row["error_message"],
        )

    def create_generation(
        self,
        *,
        project_id: str,
        catalog_id: str,
        source_uri: str,
        source_sha256: str,
        source_format: SourceFormat,
        batch_size: int,
        max_rows: int,
        feature_dim: int,
        ngram_min: int,
        ngram_max: int,
        auto_activate: bool,
    ) -> AtomicCatalogGeneration:
        generation_id = "gen_" + uuid.uuid4().hex[:24]
        now = _utc_now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO atomic_catalog_generations (
                    project_id, catalog_id, generation_id, status, source_uri,
                    source_sha256, source_format, batch_size, max_rows, feature_dim,
                    ngram_min, ngram_max, auto_activate, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    catalog_id,
                    generation_id,
                    source_uri,
                    source_sha256,
                    source_format,
                    batch_size,
                    max_rows,
                    feature_dim,
                    ngram_min,
                    ngram_max,
                    1 if auto_activate else 0,
                    now,
                    now,
                ),
            )
        return self.get_generation(project_id, catalog_id, generation_id)

    def get_generation(self, project_id: str, catalog_id: str, generation_id: str) -> AtomicCatalogGeneration:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM atomic_catalog_generations WHERE project_id=? AND catalog_id=? AND generation_id=?",
                (project_id, catalog_id, generation_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("atomic catalog generation not found")
            return self._generation(row)

    def list_generations(self, project_id: str, catalog_id: str, limit: int = 100) -> list[AtomicCatalogGeneration]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM atomic_catalog_generations WHERE project_id=? AND catalog_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (project_id, catalog_id, limit),
            ).fetchall()
            return [self._generation(row) for row in rows]

    def get_active(self, project_id: str, catalog_id: str) -> AtomicCatalogActive | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM atomic_catalog_active WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()
            if row is None:
                return None
            return AtomicCatalogActive(
                project_id=project_id,
                catalog_id=catalog_id,
                generation_id=row["generation_id"],
                previous_generation_id=row["previous_generation_id"],
                revision=int(row["revision"]),
                updated_at=row["updated_at"],
            )

    def claim_next(self, worker_id: str, lease_seconds: int = 300) -> AtomicCatalogGeneration | None:
        now = _utc_now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """SELECT project_id, catalog_id, generation_id
                    FROM atomic_catalog_generations
                    WHERE status='queued' OR (
                        status='building' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                    ) ORDER BY created_at ASC LIMIT 1""",
                    (now,),
                ).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                self._db.execute(
                    "UPDATE atomic_catalog_generations SET status='building', worker_id=?, lease_expires_at=?, "
                    "updated_at=? WHERE project_id=? AND catalog_id=? AND generation_id=?",
                    (
                        worker_id,
                        _future(lease_seconds),
                        now,
                        row["project_id"],
                        row["catalog_id"],
                        row["generation_id"],
                    ),
                )
                self._db.commit()
                return self.get_generation(row["project_id"], row["catalog_id"], row["generation_id"])
            except Exception:
                self._db.rollback()
                raise

    def heartbeat(self, generation: AtomicCatalogGeneration, worker_id: str, lease_seconds: int = 300) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE atomic_catalog_generations SET lease_expires_at=?, updated_at=? "
                "WHERE project_id=? AND catalog_id=? AND generation_id=? AND worker_id=? AND status='building'",
                (
                    _future(lease_seconds),
                    _utc_now(),
                    generation.project_id,
                    generation.catalog_id,
                    generation.generation_id,
                    worker_id,
                ),
            )

    def mark_ready(
        self,
        generation: AtomicCatalogGeneration,
        *,
        artifact_uri: str,
        artifact_sha256: str,
        item_count: int,
        feature_rows: int,
        database_bytes: int,
        build_seconds: float,
    ) -> AtomicCatalogGeneration:
        now = _utc_now()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE atomic_catalog_generations SET status='ready', artifact_uri=?, artifact_sha256=?,
                item_count=?, feature_rows=?, database_bytes=?, build_seconds=?, ready_at=?, updated_at=?,
                worker_id=NULL, lease_expires_at=NULL, error_message=NULL
                WHERE project_id=? AND catalog_id=? AND generation_id=?""",
                (
                    artifact_uri,
                    artifact_sha256,
                    item_count,
                    feature_rows,
                    database_bytes,
                    build_seconds,
                    now,
                    now,
                    generation.project_id,
                    generation.catalog_id,
                    generation.generation_id,
                ),
            )
        return self.get_generation(generation.project_id, generation.catalog_id, generation.generation_id)

    def fail(self, generation: AtomicCatalogGeneration, message: str) -> None:
        now = _utc_now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE atomic_catalog_generations SET status='failed', error_message=?, worker_id=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE project_id=? AND catalog_id=? AND generation_id=?",
                (message[:4000], now, generation.project_id, generation.catalog_id, generation.generation_id),
            )

    def activate(self, project_id: str, catalog_id: str, generation_id: str, reason: str = "activate") -> AtomicCatalogActivation:
        started = time.perf_counter()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                target = self._db.execute(
                    "SELECT status FROM atomic_catalog_generations WHERE project_id=? AND catalog_id=? AND generation_id=?",
                    (project_id, catalog_id, generation_id),
                ).fetchone()
                if target is None:
                    raise FileNotFoundError("atomic catalog generation not found")
                if target["status"] not in {"ready", "active", "retired"}:
                    raise ValueError(f"generation cannot be activated from status={target['status']}")
                current = self._db.execute(
                    "SELECT generation_id, revision FROM atomic_catalog_active WHERE project_id=? AND catalog_id=?",
                    (project_id, catalog_id),
                ).fetchone()
                previous = current["generation_id"] if current else None
                revision = (int(current["revision"]) + 1) if current else 1
                now = _utc_now()
                if previous and previous != generation_id:
                    self._db.execute(
                        "UPDATE atomic_catalog_generations SET status='retired', updated_at=? "
                        "WHERE project_id=? AND catalog_id=? AND generation_id=?",
                        (now, project_id, catalog_id, previous),
                    )
                self._db.execute(
                    "UPDATE atomic_catalog_generations SET status='active', activated_at=?, updated_at=? "
                    "WHERE project_id=? AND catalog_id=? AND generation_id=?",
                    (now, now, project_id, catalog_id, generation_id),
                )
                self._db.execute(
                    """INSERT INTO atomic_catalog_active
                    (project_id, catalog_id, generation_id, previous_generation_id, revision, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, catalog_id) DO UPDATE SET
                        generation_id=excluded.generation_id,
                        previous_generation_id=excluded.previous_generation_id,
                        revision=excluded.revision,
                        updated_at=excluded.updated_at""",
                    (project_id, catalog_id, generation_id, previous, revision, now),
                )
                self._db.execute(
                    "INSERT INTO atomic_catalog_deployments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_id, catalog_id, revision, previous, generation_id, reason, now),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        active = self.get_active(project_id, catalog_id)
        assert active is not None
        return AtomicCatalogActivation(
            active=active,
            previous_generation_id=previous,
            swap_latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            prewarmed=True,
        )

    def rollback(self, project_id: str, catalog_id: str) -> AtomicCatalogActivation:
        active = self.get_active(project_id, catalog_id)
        if active is None or not active.previous_generation_id:
            raise ValueError("no previous atomic generation is available for rollback")
        return self.activate(
            project_id,
            catalog_id,
            active.previous_generation_id,
            reason=f"rollback_from:{active.generation_id}",
        )


class ImmutableCatalogBuilder:
    def __init__(self, build_dir: str | Path | None = None):
        self.build_dir = Path(build_dir or os.getenv("RTDC_ATOMIC_BUILD_DIR", "data/atomic-build"))
        self.build_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _metadata_from_csv(row: dict[str, str | None]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        raw = row.get("metadata_json") or row.get("metadata")
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("metadata_json must contain an object")
            metadata.update(parsed)
        for key, value in row.items():
            if key and key.startswith("meta_") and value not in {None, ""}:
                metadata[key[5:]] = value
        return metadata

    def iter_source(self, path: Path, source_format: SourceFormat) -> Iterable[CandidateCatalogItem]:
        if source_format == "jsonl":
            with path.open("r", encoding="utf-8") as handle:
                for physical, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                        if not isinstance(raw, dict):
                            raise ValueError("row must be an object")
                        yield CandidateCatalogItem.model_validate(raw)
                    except Exception as exc:
                        raise ValueError(f"invalid JSONL row at line {physical}: {exc}") from exc
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "id" not in reader.fieldnames or "text" not in reader.fieldnames:
                raise ValueError("CSV requires id and text columns")
            for physical, row in enumerate(reader, start=2):
                try:
                    yield CandidateCatalogItem(
                        id=(row.get("id") or "").strip(),
                        text=row.get("text") or "",
                        metadata=self._metadata_from_csv(row),
                    )
                except Exception as exc:
                    raise ValueError(f"invalid CSV row at line {physical}: {exc}") from exc

    def build(
        self,
        *,
        source_path: Path,
        source_format: SourceFormat,
        generation_id: str,
        batch_size: int,
        max_rows: int,
        feature_dim: int,
        ngram_min: int,
        ngram_max: int,
        heartbeat=None,
    ) -> AtomicCatalogBuildResult:
        started = time.perf_counter()
        db_path = self.build_dir / f"{generation_id}.sqlite3"
        db_path.unlink(missing_ok=True)
        db = sqlite3.connect(db_path)
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA locking_mode=EXCLUSIVE")
            db.execute("PRAGMA cache_size=-200000")
            db.execute("CREATE TABLE items (item_id TEXT PRIMARY KEY, text TEXT NOT NULL, metadata_json TEXT NOT NULL) WITHOUT ROWID")
            db.execute("CREATE TABLE features (feature_id INTEGER NOT NULL, item_id TEXT NOT NULL, weight REAL NOT NULL)")
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")

            items_batch: list[tuple[str, str, str]] = []
            feature_batch: list[tuple[int, str, float]] = []
            item_count = 0
            feature_rows = 0
            last_heartbeat = time.monotonic()

            def flush() -> None:
                nonlocal feature_rows
                if not items_batch:
                    return
                with db:
                    db.executemany("INSERT INTO items(item_id, text, metadata_json) VALUES (?, ?, ?)", items_batch)
                    db.executemany("INSERT INTO features(feature_id, item_id, weight) VALUES (?, ?, ?)", feature_batch)
                feature_rows += len(feature_batch)
                items_batch.clear()
                feature_batch.clear()

            for item in self.iter_source(source_path, source_format):
                item_count += 1
                if item_count > max_rows:
                    raise ValueError(f"generation exceeds max_rows={max_rows}")
                items_batch.append((item.id, item.text, json.dumps(item.metadata, ensure_ascii=False, sort_keys=True)))
                signature = sparse_signature(item.text, feature_dim, ngram_min, ngram_max)
                feature_batch.extend((int(fid), item.id, float(weight)) for fid, weight in signature.items())
                if len(items_batch) >= batch_size:
                    flush()
                    if heartbeat and time.monotonic() - last_heartbeat >= 20:
                        heartbeat()
                        last_heartbeat = time.monotonic()
            flush()
            if item_count == 0:
                raise ValueError("atomic catalog source contains no candidates")

            db.execute("CREATE INDEX idx_features_lookup ON features(feature_id, item_id)")
            db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("item_count", str(item_count)),
                    ("feature_rows", str(feature_rows)),
                    ("feature_dim", str(feature_dim)),
                    ("ngram_min", str(ngram_min)),
                    ("ngram_max", str(ngram_max)),
                    ("format_version", "1"),
                ],
            )
            db.execute("ANALYZE")
            db.commit()
        except Exception:
            db.close()
            db_path.unlink(missing_ok=True)
            raise
        finally:
            try:
                db.close()
            except Exception:
                pass
        return AtomicCatalogBuildResult(
            database_path=str(db_path),
            item_count=item_count,
            feature_rows=feature_rows,
            database_bytes=db_path.stat().st_size,
            build_seconds=round(time.perf_counter() - started, 3),
        )


class ImmutableCatalogSearcher:
    def __init__(self, path: Path, generation: AtomicCatalogGeneration):
        self.path = path
        self.generation = generation
        self._lock = threading.RLock()
        uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
        self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("rtdc_metadata_match", 2, self._metadata_match, deterministic=True)

    @staticmethod
    def _metadata_match(raw: str, expected_raw: str) -> int:
        try:
            metadata = json.loads(raw)
            expected = json.loads(expected_raw)
            return int(all(metadata.get(key) == value for key, value in expected.items()))
        except Exception:
            return 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def local_candidates(
        self,
        request: CandidateCatalogSearchRequest,
        candidate_limit: int,
    ) -> tuple[int, list[_StoredCandidate]]:
        signature = sparse_signature(
            request.query,
            self.generation.feature_dim,
            self.generation.ngram_min,
            self.generation.ngram_max,
        )
        if not signature:
            return 0, []
        with self._lock:
            self._db.execute("DROP TABLE IF EXISTS temp.rtdc_query_features")
            self._db.execute(
                "CREATE TEMP TABLE rtdc_query_features(feature_id INTEGER PRIMARY KEY, q_weight REAL NOT NULL) WITHOUT ROWID"
            )
            self._db.executemany(
                "INSERT INTO rtdc_query_features(feature_id, q_weight) VALUES (?, ?)",
                [(int(fid), float(weight)) for fid, weight in signature.items()],
            )
            overlap = int(
                self._db.execute(
                    "SELECT COUNT(DISTINCT f.item_id) AS n FROM features f "
                    "JOIN rtdc_query_features q ON q.feature_id=f.feature_id"
                ).fetchone()["n"]
            )
            rows = self._db.execute(
                """SELECT f.item_id, SUM(f.weight * q.q_weight) AS score, i.text, i.metadata_json
                FROM features f
                JOIN rtdc_query_features q ON q.feature_id=f.feature_id
                JOIN items i ON i.item_id=f.item_id
                WHERE rtdc_metadata_match(i.metadata_json, ?) = 1
                GROUP BY f.item_id
                HAVING score >= ?
                ORDER BY score DESC, f.item_id ASC
                LIMIT ?""",
                (
                    json.dumps(request.metadata_equals, ensure_ascii=False, sort_keys=True),
                    request.min_score,
                    candidate_limit,
                ),
            ).fetchall()
            self._db.execute("DROP TABLE temp.rtdc_query_features")
            candidates = [
                _StoredCandidate(
                    id=row["item_id"],
                    text=row["text"],
                    metadata=json.loads(row["metadata_json"]),
                    stage1_score=max(0.0, min(1.0, float(row["score"]))),
                )
                for row in rows
            ]
            return overlap, candidates


class AtomicCatalogRuntime:
    def __init__(
        self,
        control: AtomicCatalogControlStore,
        storage: SharedObjectStore,
        operations: OperationalDecisionEngine,
    ):
        self.control = control
        self.storage = storage
        self.operations = operations
        self._lock = threading.RLock()
        self._searchers: dict[tuple[str, str, str], ImmutableCatalogSearcher] = {}

    def has_active(self, project_id: str, catalog_id: str) -> bool:
        return self.control.get_active(project_id, catalog_id) is not None

    def prewarm(self, generation: AtomicCatalogGeneration) -> Path:
        if not generation.artifact_uri or not generation.artifact_sha256:
            raise ValueError("generation has no ready artifact")
        return self.storage.materialize(generation.artifact_uri, generation.artifact_sha256)

    def _searcher(self, project_id: str, catalog_id: str) -> ImmutableCatalogSearcher:
        active = self.control.get_active(project_id, catalog_id)
        if active is None:
            raise FileNotFoundError("no active atomic catalog generation")
        generation = self.control.get_generation(project_id, catalog_id, active.generation_id)
        key = (project_id, catalog_id, generation.generation_id)
        with self._lock:
            found = self._searchers.get(key)
            if found is not None:
                return found
            path = self.prewarm(generation)
            searcher = ImmutableCatalogSearcher(path, generation)
            self._searchers[key] = searcher
            return searcher

    async def search(
        self,
        project_id: str,
        catalog_id: str,
        request: CandidateCatalogSearchRequest,
    ) -> CandidateCatalogSearchResponse:
        started = time.perf_counter()
        searcher = self._searcher(project_id, catalog_id)
        stage1_limit = max(
            request.top_k,
            request.external_rerank_k if request.final_method == "openai_compatible" else request.top_k,
        )
        overlap_count, candidates = await asyncio.to_thread(
            searcher.local_candidates,
            request,
            stage1_limit,
        )
        eligible_count = len(candidates)
        final_rows: list[tuple[float, _StoredCandidate]] = [
            (item.stage1_score, item) for item in candidates
        ]
        reranked_count = 0
        if request.final_method == "openai_compatible" and candidates:
            bounded = candidates[: request.external_rerank_k]
            ranked = await self.operations.rank(
                RankRequest(
                    query=request.query,
                    candidates=[RankCandidate(id=item.id, text=item.text) for item in bounded],
                    top_k=len(bounded),
                    method="openai_compatible",
                )
            )
            model_scores = {item.id: item.score for item in ranked.results}
            reranked_count = len(model_scores)
            final_rows = []
            for item in candidates:
                model_score = model_scores.get(item.id)
                score = item.stage1_score if model_score is None else (
                    (1.0 - request.rerank_weight) * item.stage1_score
                    + request.rerank_weight * model_score
                )
                final_rows.append((float(score), item))
        final_rows.sort(key=lambda row: (-row[0], -row[1].stage1_score, row[1].id))
        top = final_rows[: request.top_k]
        top_score = float(top[0][0]) if top else 0.0
        second_score = float(top[1][0]) if len(top) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        selected_id = top[0][1].id if top else None
        requires_review = (
            selected_id is None
            or top_score < request.min_selection_score
            or margin < request.min_selection_margin
        )
        return CandidateCatalogSearchResponse(
            catalog_id=catalog_id,
            selected_id=selected_id,
            requires_review=requires_review,
            selection_signal=round(top_score, 6),
            score_margin=round(margin, 6),
            items_indexed=searcher.generation.item_count,
            candidates_with_feature_overlap=overlap_count,
            candidates_eligible=eligible_count,
            candidates_reranked=reranked_count,
            results=[
                CandidateCatalogSearchItem(
                    id=item.id,
                    rank=index + 1,
                    stage1_score=round(item.stage1_score, 6),
                    final_score=round(score, 6),
                    metadata=item.metadata,
                    text=item.text if request.include_text else None,
                )
                for index, (score, item) in enumerate(top)
            ],
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            final_method=request.final_method,
            index_method="immutable_atomic_sparse_unicode_char_ngram",
        )

    def close(self) -> None:
        with self._lock:
            for searcher in self._searchers.values():
                searcher.close()
            self._searchers.clear()


class AtomicCatalogWorker:
    def __init__(
        self,
        control: AtomicCatalogControlStore,
        storage: SharedObjectStore,
        builder: ImmutableCatalogBuilder,
    ):
        self.control = control
        self.storage = storage
        self.builder = builder
        self.worker_id = "atomic_" + uuid.uuid4().hex[:16]
        self.poll_seconds = max(0.1, float(os.getenv("RTDC_ATOMIC_CATALOG_POLL_SECONDS", "1")))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run_loop(), name="atomic-catalog-builder")

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
            generation = await asyncio.to_thread(self.control.claim_next, self.worker_id)
            if generation is None:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            await asyncio.to_thread(self.process_generation, generation)

    def process_generation(self, generation: AtomicCatalogGeneration) -> None:
        local_db: Path | None = None
        try:
            source_path = self.storage.materialize(generation.source_uri, generation.source_sha256)
            result = self.builder.build(
                source_path=source_path,
                source_format=generation.source_format,
                generation_id=generation.generation_id,
                batch_size=generation.batch_size,
                max_rows=generation.max_rows,
                feature_dim=generation.feature_dim,
                ngram_min=generation.ngram_min,
                ngram_max=generation.ngram_max,
                heartbeat=lambda: self.control.heartbeat(generation, self.worker_id),
            )
            local_db = Path(result.database_path)
            object_ref = self.storage.put_file(
                local_db,
                f"atomic/{generation.project_id}/{generation.catalog_id}/{generation.generation_id}.sqlite3",
                content_type="application/vnd.sqlite3",
            )
            ready = self.control.mark_ready(
                generation,
                artifact_uri=object_ref.uri,
                artifact_sha256=object_ref.sha256,
                item_count=result.item_count,
                feature_rows=result.feature_rows,
                database_bytes=result.database_bytes,
                build_seconds=result.build_seconds,
            )
            if ready.auto_activate:
                # Materialize before pointer flip so this node cannot expose an
                # active generation whose artifact has not been verified locally.
                self.storage.materialize(ready.artifact_uri or "", ready.artifact_sha256)
                self.control.activate(ready.project_id, ready.catalog_id, ready.generation_id, reason="auto_activate")
        except Exception as exc:
            self.control.fail(generation, f"{type(exc).__name__}: {exc}")
        finally:
            if local_db is not None:
                local_db.unlink(missing_ok=True)


class AtomicCatalogServices:
    def __init__(self, operations: OperationalDecisionEngine):
        self.control = AtomicCatalogControlStore()
        self.storage = SharedObjectStore.from_env()
        self.builder = ImmutableCatalogBuilder()
        self.runtime = AtomicCatalogRuntime(self.control, self.storage, operations)
        self.worker = AtomicCatalogWorker(self.control, self.storage, self.builder)

    def activate(self, project_id: str, catalog_id: str, generation_id: str) -> AtomicCatalogActivation:
        generation = self.control.get_generation(project_id, catalog_id, generation_id)
        self.runtime.prewarm(generation)
        return self.control.activate(project_id, catalog_id, generation_id)

    def rollback(self, project_id: str, catalog_id: str) -> AtomicCatalogActivation:
        active = self.control.get_active(project_id, catalog_id)
        if active is None or not active.previous_generation_id:
            raise ValueError("no previous atomic generation is available for rollback")
        generation = self.control.get_generation(project_id, catalog_id, active.previous_generation_id)
        self.runtime.prewarm(generation)
        return self.control.rollback(project_id, catalog_id)

    async def close(self) -> None:
        await self.worker.stop()
        self.runtime.close()
        self.control.close()
