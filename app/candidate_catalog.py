from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.operation_models import RankCandidate, RankRequest
from app.operations import OperationalDecisionEngine
from app.semantic_matrix import sparse_signature


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandidateCatalogCreate(BaseModel):
    catalog_id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    feature_dim: int = Field(default=32768, ge=1024, le=262144)
    ngram_min: int = Field(default=2, ge=1, le=5)
    ngram_max: int = Field(default=4, ge=1, le=6)

    @model_validator(mode="after")
    def validate_ngrams(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        return self


class CandidateCatalogSummary(BaseModel):
    project_id: str
    catalog_id: str
    name: str
    item_count: int
    feature_dim: int
    ngram_min: int
    ngram_max: int
    created_at: str
    updated_at: str


class CandidateCatalogItem(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateCatalogUpsertRequest(BaseModel):
    items: list[CandidateCatalogItem] = Field(min_length=1, max_length=5000)
    max_total_chars: int = Field(default=10_000_000, ge=1_000, le=50_000_000)

    @model_validator(mode="after")
    def validate_batch(self):
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("item ids must be unique within an upsert batch")
        total = sum(len(item.text) for item in self.items)
        if total > self.max_total_chars:
            raise ValueError(
                f"catalog upsert text is too large: {total} chars exceeds max_total_chars={self.max_total_chars}"
            )
        return self


class CandidateCatalogDeleteItemsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(self.ids) != len(set(self.ids)):
            raise ValueError("item ids must be unique")
        if any(not item_id or len(item_id) > 200 for item_id in self.ids):
            raise ValueError("item ids must be between 1 and 200 characters")
        return self


class CandidateCatalogMutationResponse(BaseModel):
    catalog_id: str
    affected_items: int
    item_count: int
    updated_at: str


class CandidateCatalogSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    metadata_equals: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=10, ge=1, le=100)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    final_method: Literal["local_sparse", "openai_compatible"] = "local_sparse"
    external_rerank_k: int = Field(default=30, ge=1, le=50)
    rerank_weight: float = Field(default=0.75, ge=0.0, le=1.0)
    min_selection_score: float = Field(default=0.15, ge=0.0, le=1.0)
    min_selection_margin: float = Field(default=0.02, ge=0.0, le=1.0)
    include_text: bool = False


class CandidateCatalogSearchItem(BaseModel):
    id: str
    rank: int
    stage1_score: float
    final_score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None


class CandidateCatalogSearchResponse(BaseModel):
    catalog_id: str
    selected_id: str | None
    requires_review: bool
    selection_signal: float
    score_margin: float
    items_indexed: int
    candidates_with_feature_overlap: int
    candidates_eligible: int
    candidates_reranked: int
    results: list[CandidateCatalogSearchItem]
    latency_ms: float
    final_method: str
    index_method: str = "sqlite_inverted_sparse_unicode_char_ngram"
    calibration_note: str = "selection_signal is a routing heuristic, not an empirical probability of correctness"


class _StoredCandidate(BaseModel):
    id: str
    text: str
    metadata: dict[str, Any]
    stage1_score: float


class CandidateCatalogStore:
    """Project-scoped persistent catalog with a SQLite inverted sparse-feature index."""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_CANDIDATE_CATALOG_DB", "data/candidate_catalogs.sqlite3")
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_catalogs (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    feature_dim INTEGER NOT NULL,
                    ngram_min INTEGER NOT NULL,
                    ngram_max INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_id)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_catalog_items (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_id, item_id),
                    FOREIGN KEY(project_id, catalog_id)
                        REFERENCES candidate_catalogs(project_id, catalog_id) ON DELETE CASCADE
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_catalog_features (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    feature_id INTEGER NOT NULL,
                    weight REAL NOT NULL,
                    PRIMARY KEY(project_id, catalog_id, item_id, feature_id),
                    FOREIGN KEY(project_id, catalog_id, item_id)
                        REFERENCES candidate_catalog_items(project_id, catalog_id, item_id) ON DELETE CASCADE
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_feature_lookup "
                "ON candidate_catalog_features(project_id, catalog_id, feature_id)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_item_catalog "
                "ON candidate_catalog_items(project_id, catalog_id, item_id)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _catalog_row(self, project_id: str, catalog_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM candidate_catalogs WHERE project_id=? AND catalog_id=?",
            (project_id, catalog_id),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("candidate catalog not found")
        return row

    def _summary_from_row(self, row: sqlite3.Row) -> CandidateCatalogSummary:
        count_row = self._db.execute(
            "SELECT COUNT(*) AS n FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
            (row["project_id"], row["catalog_id"]),
        ).fetchone()
        return CandidateCatalogSummary(
            project_id=row["project_id"],
            catalog_id=row["catalog_id"],
            name=row["name"],
            item_count=int(count_row["n"]),
            feature_dim=int(row["feature_dim"]),
            ngram_min=int(row["ngram_min"]),
            ngram_max=int(row["ngram_max"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_catalog(self, project_id: str, payload: CandidateCatalogCreate) -> CandidateCatalogSummary:
        now = _utc_now()
        with self._lock, self._db:
            try:
                self._db.execute(
                    "INSERT INTO candidate_catalogs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        payload.catalog_id,
                        payload.name,
                        payload.feature_dim,
                        payload.ngram_min,
                        payload.ngram_max,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FileExistsError("candidate catalog already exists") from exc
            row = self._catalog_row(project_id, payload.catalog_id)
            return self._summary_from_row(row)

    def list_catalogs(self, project_id: str, limit: int = 100) -> list[CandidateCatalogSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM candidate_catalogs WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
            return [self._summary_from_row(row) for row in rows]

    def get_catalog(self, project_id: str, catalog_id: str) -> CandidateCatalogSummary:
        with self._lock:
            return self._summary_from_row(self._catalog_row(project_id, catalog_id))

    def delete_catalog(self, project_id: str, catalog_id: str) -> bool:
        with self._lock, self._db:
            self._catalog_row(project_id, catalog_id)
            cursor = self._db.execute(
                "DELETE FROM candidate_catalogs WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            )
            return cursor.rowcount > 0

    def upsert_items(
        self,
        project_id: str,
        catalog_id: str,
        payload: CandidateCatalogUpsertRequest,
    ) -> CandidateCatalogMutationResponse:
        with self._lock, self._db:
            catalog = self._catalog_row(project_id, catalog_id)
            now = _utc_now()
            for item in payload.items:
                existing = self._db.execute(
                    "SELECT created_at FROM candidate_catalog_items "
                    "WHERE project_id=? AND catalog_id=? AND item_id=?",
                    (project_id, catalog_id, item.id),
                ).fetchone()
                created_at = existing["created_at"] if existing else now
                self._db.execute(
                    "INSERT INTO candidate_catalog_items "
                    "(project_id, catalog_id, item_id, text, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_id, catalog_id, item_id) DO UPDATE SET "
                    "text=excluded.text, metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
                    (
                        project_id,
                        catalog_id,
                        item.id,
                        item.text,
                        json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                        created_at,
                        now,
                    ),
                )
                self._db.execute(
                    "DELETE FROM candidate_catalog_features "
                    "WHERE project_id=? AND catalog_id=? AND item_id=?",
                    (project_id, catalog_id, item.id),
                )
                signature = sparse_signature(
                    item.text,
                    int(catalog["feature_dim"]),
                    int(catalog["ngram_min"]),
                    int(catalog["ngram_max"]),
                )
                if signature:
                    self._db.executemany(
                        "INSERT INTO candidate_catalog_features "
                        "(project_id, catalog_id, item_id, feature_id, weight) VALUES (?, ?, ?, ?, ?)",
                        [
                            (project_id, catalog_id, item.id, int(feature_id), float(weight))
                            for feature_id, weight in signature.items()
                        ],
                    )
            self._db.execute(
                "UPDATE candidate_catalogs SET updated_at=? WHERE project_id=? AND catalog_id=?",
                (now, project_id, catalog_id),
            )
            summary = self._summary_from_row(self._catalog_row(project_id, catalog_id))
            return CandidateCatalogMutationResponse(
                catalog_id=catalog_id,
                affected_items=len(payload.items),
                item_count=summary.item_count,
                updated_at=summary.updated_at,
            )

    def delete_items(
        self,
        project_id: str,
        catalog_id: str,
        payload: CandidateCatalogDeleteItemsRequest,
    ) -> CandidateCatalogMutationResponse:
        with self._lock, self._db:
            self._catalog_row(project_id, catalog_id)
            affected = 0
            for item_id in payload.ids:
                cursor = self._db.execute(
                    "DELETE FROM candidate_catalog_items WHERE project_id=? AND catalog_id=? AND item_id=?",
                    (project_id, catalog_id, item_id),
                )
                affected += max(0, cursor.rowcount)
            now = _utc_now()
            self._db.execute(
                "UPDATE candidate_catalogs SET updated_at=? WHERE project_id=? AND catalog_id=?",
                (now, project_id, catalog_id),
            )
            summary = self._summary_from_row(self._catalog_row(project_id, catalog_id))
            return CandidateCatalogMutationResponse(
                catalog_id=catalog_id,
                affected_items=affected,
                item_count=summary.item_count,
                updated_at=summary.updated_at,
            )

    @staticmethod
    def _chunks(values: list[int] | list[str], size: int = 400):
        for start in range(0, len(values), size):
            yield values[start : start + size]

    def local_candidates(
        self,
        project_id: str,
        catalog_id: str,
        request: CandidateCatalogSearchRequest,
        candidate_limit: int,
    ) -> tuple[CandidateCatalogSummary, int, list[_StoredCandidate]]:
        with self._lock:
            catalog_row = self._catalog_row(project_id, catalog_id)
            summary = self._summary_from_row(catalog_row)
            query_signature = sparse_signature(
                request.query,
                int(catalog_row["feature_dim"]),
                int(catalog_row["ngram_min"]),
                int(catalog_row["ngram_max"]),
            )
            if not query_signature:
                return summary, 0, []

            dot: dict[str, float] = defaultdict(float)
            feature_ids = list(query_signature)
            for chunk in self._chunks(feature_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    "SELECT item_id, feature_id, weight FROM candidate_catalog_features "
                    f"WHERE project_id=? AND catalog_id=? AND feature_id IN ({placeholders})",
                    (project_id, catalog_id, *chunk),
                ).fetchall()
                for row in rows:
                    dot[row["item_id"]] += float(row["weight"]) * query_signature[int(row["feature_id"])]

            overlap_count = len(dot)
            if not dot:
                return summary, 0, []

            stored: dict[str, sqlite3.Row] = {}
            ids = list(dot)
            for chunk in self._chunks(ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    "SELECT item_id, text, metadata_json FROM candidate_catalog_items "
                    f"WHERE project_id=? AND catalog_id=? AND item_id IN ({placeholders})",
                    (project_id, catalog_id, *chunk),
                ).fetchall()
                stored.update({row["item_id"]: row for row in rows})

            ranked: list[_StoredCandidate] = []
            for item_id, score in dot.items():
                if score < request.min_score:
                    continue
                row = stored.get(item_id)
                if row is None:
                    continue
                metadata = json.loads(row["metadata_json"])
                if any(metadata.get(key) != value for key, value in request.metadata_equals.items()):
                    continue
                ranked.append(
                    _StoredCandidate(
                        id=item_id,
                        text=row["text"],
                        metadata=metadata,
                        stage1_score=max(0.0, min(1.0, float(score))),
                    )
                )

            ranked.sort(key=lambda item: (-item.stage1_score, item.id))
            return summary, overlap_count, ranked[:candidate_limit]


class CandidateCatalogEngine:
    def __init__(self, store: CandidateCatalogStore, operations: OperationalDecisionEngine):
        self.store = store
        self.operations = operations

    async def search(
        self,
        project_id: str,
        catalog_id: str,
        request: CandidateCatalogSearchRequest,
    ) -> CandidateCatalogSearchResponse:
        started = time.perf_counter()
        stage1_limit = max(
            request.top_k,
            request.external_rerank_k if request.final_method == "openai_compatible" else request.top_k,
        )
        summary, overlap_count, candidates = self.store.local_candidates(
            project_id, catalog_id, request, stage1_limit
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
            items_indexed=summary.item_count,
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
        )
