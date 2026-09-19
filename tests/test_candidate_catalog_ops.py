import pytest

from app.candidate_catalog import (
    CandidateCatalogCreate,
    CandidateCatalogDeleteItemsRequest,
    CandidateCatalogEngine,
    CandidateCatalogSearchRequest,
    CandidateCatalogStore,
    CandidateCatalogUpsertRequest,
)
from app.candidate_catalog_ops import (
    CandidateCatalogSnapshotCreate,
    CandidateCatalogSnapshotManager,
)
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine


@pytest.mark.asyncio
async def test_snapshot_restore_is_transactional_and_searchable(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    snapshots = CandidateCatalogSnapshotManager(store)
    engine = CandidateCatalogEngine(store, OperationalDecisionEngine(DecisionEngine()))
    try:
        store.create_catalog("p1", CandidateCatalogCreate(catalog_id="support", name="Support"))
        store.upsert_items(
            "p1",
            "support",
            CandidateCatalogUpsertRequest(
                items=[
                    {"id": "checkin", "text": "新宿 宿泊 チェックイン 入室 方法"},
                    {"id": "wifi", "text": "WiFi パスワード インターネット 接続"},
                ]
            ),
        )
        snapshot = snapshots.create_snapshot(
            "p1", "support", CandidateCatalogSnapshotCreate(note="before campaign update")
        )
        assert snapshot.item_count == 2

        store.upsert_items(
            "p1",
            "support",
            CandidateCatalogUpsertRequest(
                items=[
                    {"id": "checkin", "text": "駐車場 アクセス"},
                    {"id": "billing", "text": "請求書 クレジットカード 支払い"},
                ]
            ),
        )
        store.delete_items(
            "p1", "support", CandidateCatalogDeleteItemsRequest(ids=["wifi"])
        )

        restored = snapshots.restore_snapshot("p1", "support", snapshot.snapshot_id)
        assert restored.item_count == 2
        assert snapshots.integrity("p1", "support").ok is True

        result = await engine.search(
            "p1",
            "support",
            CandidateCatalogSearchRequest(
                query="チェックイン方法",
                top_k=2,
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert result.selected_id == "checkin"
        assert {item.id for item in result.results}.issubset({"checkin", "wifi"})
    finally:
        store.close()


def test_integrity_detects_missing_index_and_rebuild_repairs_it(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    snapshots = CandidateCatalogSnapshotManager(store)
    try:
        store.create_catalog("p1", CandidateCatalogCreate(catalog_id="products", name="Products"))
        store.upsert_items(
            "p1",
            "products",
            CandidateCatalogUpsertRequest(
                items=[
                    {"id": "a", "text": "alpha product"},
                    {"id": "b", "text": "beta product"},
                ]
            ),
        )
        assert snapshots.integrity("p1", "products").ok is True

        with store._lock, store._db:
            store._db.execute(
                "DELETE FROM candidate_catalog_features "
                "WHERE project_id=? AND catalog_id=? AND item_id=?",
                ("p1", "products", "a"),
            )

        broken = snapshots.integrity("p1", "products")
        assert broken.ok is False
        assert broken.items_without_features == 1

        rebuilt = snapshots.rebuild_index("p1", "products")
        assert rebuilt.item_count == 2
        repaired = snapshots.integrity("p1", "products")
        assert repaired.ok is True
        assert repaired.indexed_item_count == 2
    finally:
        store.close()


def test_snapshot_isolation_and_delete(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    snapshots = CandidateCatalogSnapshotManager(store)
    try:
        store.create_catalog("project_a", CandidateCatalogCreate(catalog_id="items", name="A"))
        snap = snapshots.create_snapshot(
            "project_a", "items", CandidateCatalogSnapshotCreate(note="baseline")
        )
        with pytest.raises(FileNotFoundError):
            snapshots.list_snapshots("project_b", "items")
        with pytest.raises(FileNotFoundError):
            snapshots.restore_snapshot("project_b", "items", snap.snapshot_id)
        assert snapshots.delete_snapshot("project_a", "items", snap.snapshot_id) is True
        assert snapshots.list_snapshots("project_a", "items") == []
    finally:
        store.close()


def test_catalog_operations_routes_are_wired():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/project/candidate-catalogs/{catalog_id}/snapshots" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/snapshots/{snapshot_id}/restore" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/integrity" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/rebuild" in paths
