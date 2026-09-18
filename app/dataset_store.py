from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.dataset_models import (
    DatasetCreate,
    DatasetExample,
    DatasetExampleInput,
    DatasetModelRecord,
    DatasetSummary,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DatasetStore:
    """Governed local dataset registry for operator-authorized training data.

    The registry intentionally requires a provenance statement and rights attestation.
    Dataset examples are raw training data, so unlike the review queue they are stored
    intentionally. Production deployments should place this data on approved encrypted
    storage with access controls, backups, and retention enforcement appropriate to the
    operator's obligations.
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("RTDC_DATASET_DB", "data/datasets.sqlite3")
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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    name TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    description TEXT,
                    source_type TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    rights_attested INTEGER NOT NULL,
                    contains_personal_data INTEGER NOT NULL,
                    allow_model_training INTEGER NOT NULL,
                    retention_until TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_examples (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    label TEXT NOT NULL,
                    split TEXT NOT NULL,
                    source_ref TEXT,
                    review_id TEXT,
                    UNIQUE(dataset_id, text_sha256)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_models (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    train_examples INTEGER NOT NULL,
                    dataset_fingerprint_sha256 TEXT NOT NULL,
                    training_json TEXT NOT NULL,
                    evaluation_json TEXT
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_datasets_decision ON datasets(decision_id, created_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_dataset_examples_split ON dataset_examples(dataset_id, split)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_dataset_models_dataset ON dataset_models(dataset_id, created_at)")

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def create(self, request: DatasetCreate) -> DatasetSummary:
        now = _utc_now()
        dataset_id = "ds_" + uuid.uuid4().hex[:20]
        retention_until = now + timedelta(days=request.retention_days)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO datasets (
                    id, created_at, updated_at, name, decision_id, description,
                    source_type, provenance, purpose, rights_attested,
                    contains_personal_data, allow_model_training, retention_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id,
                    now.isoformat(),
                    now.isoformat(),
                    request.name,
                    request.decision_id,
                    request.description,
                    request.source_type,
                    request.provenance,
                    request.purpose,
                    int(request.rights_attested),
                    int(request.contains_personal_data),
                    int(request.allow_model_training),
                    retention_until.isoformat(),
                ),
            )
        return self.get(dataset_id)

    def _row_to_summary(self, row: sqlite3.Row) -> DatasetSummary:
        dataset_id = row["id"]
        with self._lock:
            counts = self._conn.execute(
                """
                SELECT split, COUNT(*) AS n
                FROM dataset_examples WHERE dataset_id = ? GROUP BY split
                """,
                (dataset_id,),
            ).fetchall()
            labels = self._conn.execute(
                """
                SELECT label, COUNT(*) AS n
                FROM dataset_examples WHERE dataset_id = ? GROUP BY label ORDER BY label
                """,
                (dataset_id,),
            ).fetchall()
            fingerprint_rows = self._conn.execute(
                """
                SELECT text_sha256, label, split
                FROM dataset_examples WHERE dataset_id = ?
                ORDER BY text_sha256, label, split
                """,
                (dataset_id,),
            ).fetchall()
        split_counts = {item["split"]: int(item["n"]) for item in counts}
        label_counts = {item["label"]: int(item["n"]) for item in labels}
        digest = hashlib.sha256()
        for item in fingerprint_rows:
            digest.update(f"{item['text_sha256']}\t{item['label']}\t{item['split']}\n".encode("utf-8"))
        return DatasetSummary(
            id=dataset_id,
            name=row["name"],
            decision_id=row["decision_id"],
            description=row["description"],
            source_type=row["source_type"],
            provenance=row["provenance"],
            purpose=row["purpose"],
            rights_attested=bool(row["rights_attested"]),
            contains_personal_data=bool(row["contains_personal_data"]),
            allow_model_training=bool(row["allow_model_training"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            retention_until=row["retention_until"],
            examples=sum(split_counts.values()),
            train_examples=split_counts.get("train", 0),
            validation_examples=split_counts.get("validation", 0),
            test_examples=split_counts.get("test", 0),
            labels=label_counts,
            fingerprint_sha256=digest.hexdigest(),
        )

    def get(self, dataset_id: str) -> DatasetSummary:
        with self._lock:
            row = self._conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"dataset not found: {dataset_id}")
        return self._row_to_summary(row)

    def list(self, decision_id: str | None = None, limit: int = 100) -> list[DatasetSummary]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if decision_id:
                rows = self._conn.execute(
                    "SELECT * FROM datasets WHERE decision_id = ? ORDER BY created_at DESC LIMIT ?",
                    (decision_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM datasets ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    def add_examples(
        self,
        dataset_id: str,
        examples: list[DatasetExampleInput],
        *,
        review_ids: list[str | None] | None = None,
    ) -> tuple[int, int]:
        self.get(dataset_id)
        now = _utc_now().isoformat()
        added = 0
        duplicates = 0
        ids = review_ids or [None] * len(examples)
        if len(ids) != len(examples):
            raise ValueError("review_ids length must match examples length")
        with self._lock, self._conn:
            for example, review_id in zip(examples, ids):
                text_hash = self._hash_text(example.text)
                try:
                    self._conn.execute(
                        """
                        INSERT INTO dataset_examples (
                            id, dataset_id, created_at, text, text_sha256, label,
                            split, source_ref, review_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "ex_" + uuid.uuid4().hex[:20],
                            dataset_id,
                            now,
                            example.text,
                            text_hash,
                            example.label,
                            example.split,
                            example.source_ref,
                            review_id,
                        ),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
            self._conn.execute("UPDATE datasets SET updated_at = ? WHERE id = ?", (now, dataset_id))
        return added, duplicates

    @staticmethod
    def _row_to_example(row: sqlite3.Row) -> DatasetExample:
        return DatasetExample(
            id=row["id"],
            dataset_id=row["dataset_id"],
            created_at=row["created_at"],
            text=row["text"],
            text_sha256=row["text_sha256"],
            label=row["label"],
            split=row["split"],
            source_ref=row["source_ref"],
            review_id=row["review_id"],
        )

    def list_examples(
        self,
        dataset_id: str,
        split: str | None = None,
        limit: int = 10_000,
    ) -> list[DatasetExample]:
        self.get(dataset_id)
        if split is not None and split not in {"train", "validation", "test"}:
            raise ValueError("split must be train, validation, test, or omitted")
        limit = max(1, min(int(limit), 50_000))
        with self._lock:
            if split:
                rows = self._conn.execute(
                    """
                    SELECT * FROM dataset_examples
                    WHERE dataset_id = ? AND split = ? ORDER BY created_at, id LIMIT ?
                    """,
                    (dataset_id, split, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM dataset_examples WHERE dataset_id = ? ORDER BY created_at, id LIMIT ?",
                    (dataset_id, limit),
                ).fetchall()
        return [self._row_to_example(row) for row in rows]

    def record_model(
        self,
        dataset_id: str,
        model_id: str,
        train_examples: int,
        fingerprint: str,
        training: dict,
        evaluation: dict | None,
    ) -> DatasetModelRecord:
        self.get(dataset_id)
        record_id = "lin_" + uuid.uuid4().hex[:20]
        created_at = _utc_now().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO dataset_models (
                    id, dataset_id, model_id, created_at, train_examples,
                    dataset_fingerprint_sha256, training_json, evaluation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    dataset_id,
                    model_id,
                    created_at,
                    train_examples,
                    fingerprint,
                    json.dumps(training, ensure_ascii=False, separators=(",", ":")),
                    None if evaluation is None else json.dumps(evaluation, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return DatasetModelRecord(
            id=record_id,
            dataset_id=dataset_id,
            model_id=model_id,
            created_at=created_at,
            train_examples=train_examples,
            dataset_fingerprint_sha256=fingerprint,
            training=training,
            evaluation=evaluation,
        )

    def list_models(self, dataset_id: str, limit: int = 100) -> list[DatasetModelRecord]:
        self.get(dataset_id)
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dataset_models WHERE dataset_id = ? ORDER BY created_at DESC LIMIT ?",
                (dataset_id, limit),
            ).fetchall()
        return [
            DatasetModelRecord(
                id=row["id"],
                dataset_id=row["dataset_id"],
                model_id=row["model_id"],
                created_at=row["created_at"],
                train_examples=int(row["train_examples"]),
                dataset_fingerprint_sha256=row["dataset_fingerprint_sha256"],
                training=json.loads(row["training_json"]),
                evaluation=None if row["evaluation_json"] is None else json.loads(row["evaluation_json"]),
            )
            for row in rows
        ]

    def delete(self, dataset_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
            return bool(cursor.rowcount)

    def purge_expired(self) -> int:
        cutoff = _utc_now().isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM datasets WHERE retention_until <= ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
