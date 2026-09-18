from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.studio_models import ReviewCreate, ReviewItem, ReviewResolve


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReviewStore:
    """Small local review queue with privacy-by-default input handling.

    Raw input text is stored only when `store_input=True`. Otherwise only a SHA-256
    digest is retained for correlation/audit. Production multi-node deployments should
    move the review queue to an approved managed database with appropriate controls.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_REVIEW_DB", "data/reviews.sqlite3")
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_items (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    input_text TEXT,
                    input_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    model_output_json TEXT NOT NULL,
                    suggested_label TEXT,
                    confidence REAL,
                    external_ref TEXT,
                    resolved_label TEXT,
                    reviewer TEXT,
                    notes TEXT,
                    retention_until TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_review_status_created ON review_items(status, created_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_review_retention ON review_items(retention_until)")

    @staticmethod
    def _to_item(row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            source=row["source"],
            decision_id=row["decision_id"],
            input_text=row["input_text"],
            input_sha256=row["input_sha256"],
            payload=json.loads(row["payload_json"]),
            model_output=json.loads(row["model_output_json"]),
            suggested_label=row["suggested_label"],
            confidence=row["confidence"],
            external_ref=row["external_ref"],
            resolved_label=row["resolved_label"],
            reviewer=row["reviewer"],
            notes=row["notes"],
            retention_until=row["retention_until"],
        )

    def create(self, request: ReviewCreate) -> ReviewItem:
        now = _utc_now()
        review_id = "rev_" + uuid.uuid4().hex[:20]
        raw = request.input_text
        input_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else None
        stored_input = raw if request.store_input else None
        retention_until = now + timedelta(days=request.retention_days)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO review_items (
                    id, created_at, updated_at, status, source, decision_id,
                    input_text, input_sha256, payload_json, model_output_json,
                    suggested_label, confidence, external_ref, resolved_label,
                    reviewer, notes, retention_until
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    review_id,
                    now.isoformat(),
                    now.isoformat(),
                    request.source,
                    request.decision_id,
                    stored_input,
                    input_hash,
                    json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(request.model_output, ensure_ascii=False, separators=(",", ":")),
                    request.suggested_label,
                    request.confidence,
                    request.external_ref,
                    retention_until.isoformat(),
                ),
            )
        return self.get(review_id)

    def get(self, review_id: str) -> ReviewItem:
        with self._lock:
            row = self._conn.execute("SELECT * FROM review_items WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"review item not found: {review_id}")
        return self._to_item(row)

    def list(self, status: str = "pending", limit: int = 100) -> list[ReviewItem]:
        if status not in {"pending", "resolved", "dismissed", "all"}:
            raise ValueError("status must be pending, resolved, dismissed, or all")
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if status == "all":
                rows = self._conn.execute(
                    "SELECT * FROM review_items ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM review_items WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
        return [self._to_item(row) for row in rows]

    def resolve(self, review_id: str, request: ReviewResolve) -> ReviewItem:
        now = _utc_now().isoformat()
        with self._lock, self._conn:
            exists = self._conn.execute("SELECT 1 FROM review_items WHERE id = ?", (review_id,)).fetchone()
            if exists is None:
                raise FileNotFoundError(f"review item not found: {review_id}")
            self._conn.execute(
                """
                UPDATE review_items
                SET status = ?, resolved_label = ?, reviewer = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (request.status, request.resolved_label, request.reviewer, request.notes, now, review_id),
            )
        return self.get(review_id)

    def purge_expired(self) -> int:
        cutoff = _utc_now().isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM review_items WHERE retention_until <= ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def export_training_examples(self, limit: int = 5000) -> list[dict[str, str]]:
        limit = max(1, min(int(limit), 50_000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT input_text, resolved_label
                FROM review_items
                WHERE status = 'resolved' AND input_text IS NOT NULL AND resolved_label IS NOT NULL
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{"text": row["input_text"], "label": row["resolved_label"]} for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
