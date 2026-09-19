from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.candidate_catalog import CandidateCatalogStore, _utc_now
from app.semantic_matrix import sparse_signature


class CandidateCatalogSnapshotSummary(BaseModel):
    snapshot_id: str
    catalog_id: str
    project_id: str
    item_count: int
    created_at: str
    note: str | None = None


class CandidateCatalogSnapshotCreate(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class CandidateCatalogIntegrityReport(BaseModel):
    catalog_id: str
    project_id: str
    item_count: int
    indexed_item_count: int
    feature_rows: int
    items_without_features: int
    orphan_feature_rows: int
    ok: bool


class CandidateCatalogRebuildResponse(BaseModel):
    catalog_id: str
    project_id: str
    item_count: int
    feature_rows: int
    rebuilt_at: str


class CandidateCatalogSnapshotManager:
    """Transactional snapshots and index maintenance for persistent candidate catalogs.

    Snapshot data is project-scoped and stored in the same SQLite database as the
    catalog so create/restore operations can be committed atomically.
    """

    def __init__(self, store: CandidateCatalogStore):
        self.store = store
        with self.store._lock, self.store._db:
            self.store._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_catalog_snapshots (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    note TEXT,
                    PRIMARY KEY(project_id, catalog_id, snapshot_id),
                    FOREIGN KEY(project_id, catalog_id)
                        REFERENCES candidate_catalogs(project_id, catalog_id) ON DELETE CASCADE
                )"""
            )
            self.store._db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_catalog_snapshot_items (
                    project_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_id, snapshot_id, item_id),
                    FOREIGN KEY(project_id, catalog_id, snapshot_id)
                        REFERENCES candidate_catalog_snapshots(project_id, catalog_id, snapshot_id)
                        ON DELETE CASCADE
                )"""
            )
            self.store._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_snapshot_lookup "
                "ON candidate_catalog_snapshots(project_id, catalog_id, created_at DESC)"
            )

    def create_snapshot(
        self,
        project_id: str,
        catalog_id: str,
        payload: CandidateCatalogSnapshotCreate,
    ) -> CandidateCatalogSnapshotSummary:
        snapshot_id = "snap_" + uuid.uuid4().hex[:20]
        now = _utc_now()
        with self.store._lock, self.store._db:
            self.store._catalog_row(project_id, catalog_id)
            self.store._db.execute(
                "INSERT INTO candidate_catalog_snapshots "
                "(project_id, catalog_id, snapshot_id, created_at, note) VALUES (?, ?, ?, ?, ?)",
                (project_id, catalog_id, snapshot_id, now, payload.note),
            )
            self.store._db.execute(
                "INSERT INTO candidate_catalog_snapshot_items "
                "(project_id, catalog_id, snapshot_id, item_id, text, metadata_json, created_at, updated_at) "
                "SELECT project_id, catalog_id, ?, item_id, text, metadata_json, created_at, updated_at "
                "FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
                (snapshot_id, project_id, catalog_id),
            )
            row = self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_snapshot_items "
                "WHERE project_id=? AND catalog_id=? AND snapshot_id=?",
                (project_id, catalog_id, snapshot_id),
            ).fetchone()
            return CandidateCatalogSnapshotSummary(
                snapshot_id=snapshot_id,
                catalog_id=catalog_id,
                project_id=project_id,
                item_count=int(row["n"]),
                created_at=now,
                note=payload.note,
            )

    def list_snapshots(
        self, project_id: str, catalog_id: str, limit: int = 100
    ) -> list[CandidateCatalogSnapshotSummary]:
        with self.store._lock:
            self.store._catalog_row(project_id, catalog_id)
            rows = self.store._db.execute(
                "SELECT s.snapshot_id, s.created_at, s.note, COUNT(i.item_id) AS item_count "
                "FROM candidate_catalog_snapshots s "
                "LEFT JOIN candidate_catalog_snapshot_items i "
                "ON i.project_id=s.project_id AND i.catalog_id=s.catalog_id AND i.snapshot_id=s.snapshot_id "
                "WHERE s.project_id=? AND s.catalog_id=? "
                "GROUP BY s.snapshot_id, s.created_at, s.note ORDER BY s.created_at DESC LIMIT ?",
                (project_id, catalog_id, limit),
            ).fetchall()
            return [
                CandidateCatalogSnapshotSummary(
                    snapshot_id=row["snapshot_id"],
                    catalog_id=catalog_id,
                    project_id=project_id,
                    item_count=int(row["item_count"]),
                    created_at=row["created_at"],
                    note=row["note"],
                )
                for row in rows
            ]

    def delete_snapshot(self, project_id: str, catalog_id: str, snapshot_id: str) -> bool:
        with self.store._lock, self.store._db:
            self.store._catalog_row(project_id, catalog_id)
            cursor = self.store._db.execute(
                "DELETE FROM candidate_catalog_snapshots "
                "WHERE project_id=? AND catalog_id=? AND snapshot_id=?",
                (project_id, catalog_id, snapshot_id),
            )
            if cursor.rowcount == 0:
                raise FileNotFoundError("candidate catalog snapshot not found")
            return True

    def restore_snapshot(
        self, project_id: str, catalog_id: str, snapshot_id: str
    ) -> CandidateCatalogRebuildResponse:
        with self.store._lock, self.store._db:
            catalog = self.store._catalog_row(project_id, catalog_id)
            snap = self.store._db.execute(
                "SELECT snapshot_id FROM candidate_catalog_snapshots "
                "WHERE project_id=? AND catalog_id=? AND snapshot_id=?",
                (project_id, catalog_id, snapshot_id),
            ).fetchone()
            if snap is None:
                raise FileNotFoundError("candidate catalog snapshot not found")

            self.store._db.execute(
                "DELETE FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            )
            self.store._db.execute(
                "INSERT INTO candidate_catalog_items "
                "(project_id, catalog_id, item_id, text, metadata_json, created_at, updated_at) "
                "SELECT project_id, catalog_id, item_id, text, metadata_json, created_at, updated_at "
                "FROM candidate_catalog_snapshot_items "
                "WHERE project_id=? AND catalog_id=? AND snapshot_id=?",
                (project_id, catalog_id, snapshot_id),
            )
            feature_rows = self._rebuild_locked(project_id, catalog_id, catalog)
            now = _utc_now()
            self.store._db.execute(
                "UPDATE candidate_catalogs SET updated_at=? WHERE project_id=? AND catalog_id=?",
                (now, project_id, catalog_id),
            )
            count = self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()
            return CandidateCatalogRebuildResponse(
                catalog_id=catalog_id,
                project_id=project_id,
                item_count=int(count["n"]),
                feature_rows=feature_rows,
                rebuilt_at=now,
            )

    def _rebuild_locked(self, project_id: str, catalog_id: str, catalog: sqlite3.Row) -> int:
        self.store._db.execute(
            "DELETE FROM candidate_catalog_features WHERE project_id=? AND catalog_id=?",
            (project_id, catalog_id),
        )
        rows = self.store._db.execute(
            "SELECT item_id, text FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
            (project_id, catalog_id),
        ).fetchall()
        inserted = 0
        for row in rows:
            signature = sparse_signature(
                row["text"],
                int(catalog["feature_dim"]),
                int(catalog["ngram_min"]),
                int(catalog["ngram_max"]),
            )
            if not signature:
                continue
            values = [
                (project_id, catalog_id, row["item_id"], int(fid), float(weight))
                for fid, weight in signature.items()
            ]
            self.store._db.executemany(
                "INSERT INTO candidate_catalog_features "
                "(project_id, catalog_id, item_id, feature_id, weight) VALUES (?, ?, ?, ?, ?)",
                values,
            )
            inserted += len(values)
        return inserted

    def rebuild_index(self, project_id: str, catalog_id: str) -> CandidateCatalogRebuildResponse:
        now = _utc_now()
        with self.store._lock, self.store._db:
            catalog = self.store._catalog_row(project_id, catalog_id)
            feature_rows = self._rebuild_locked(project_id, catalog_id, catalog)
            self.store._db.execute(
                "UPDATE candidate_catalogs SET updated_at=? WHERE project_id=? AND catalog_id=?",
                (now, project_id, catalog_id),
            )
            count = self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()
            return CandidateCatalogRebuildResponse(
                catalog_id=catalog_id,
                project_id=project_id,
                item_count=int(count["n"]),
                feature_rows=feature_rows,
                rebuilt_at=now,
            )

    def integrity(self, project_id: str, catalog_id: str) -> CandidateCatalogIntegrityReport:
        with self.store._lock:
            self.store._catalog_row(project_id, catalog_id)
            item_count = int(self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_items WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()["n"])
            feature_rows = int(self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_features WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()["n"])
            indexed_item_count = int(self.store._db.execute(
                "SELECT COUNT(DISTINCT item_id) AS n FROM candidate_catalog_features "
                "WHERE project_id=? AND catalog_id=?",
                (project_id, catalog_id),
            ).fetchone()["n"])
            orphan_feature_rows = int(self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_features f "
                "LEFT JOIN candidate_catalog_items i ON i.project_id=f.project_id "
                "AND i.catalog_id=f.catalog_id AND i.item_id=f.item_id "
                "WHERE f.project_id=? AND f.catalog_id=? AND i.item_id IS NULL",
                (project_id, catalog_id),
            ).fetchone()["n"])
            items_without_features = int(self.store._db.execute(
                "SELECT COUNT(*) AS n FROM candidate_catalog_items i "
                "LEFT JOIN candidate_catalog_features f ON f.project_id=i.project_id "
                "AND f.catalog_id=i.catalog_id AND f.item_id=i.item_id "
                "WHERE i.project_id=? AND i.catalog_id=? AND f.item_id IS NULL",
                (project_id, catalog_id),
            ).fetchone()["n"])
            return CandidateCatalogIntegrityReport(
                catalog_id=catalog_id,
                project_id=project_id,
                item_count=item_count,
                indexed_item_count=indexed_item_count,
                feature_rows=feature_rows,
                items_without_features=items_without_features,
                orphan_feature_rows=orphan_feature_rows,
                ok=orphan_feature_rows == 0 and items_without_features == 0,
            )
