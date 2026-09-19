import pytest

from app.candidate_catalog import (
    CandidateCatalogCreate,
    CandidateCatalogDeleteItemsRequest,
    CandidateCatalogEngine,
    CandidateCatalogSearchRequest,
    CandidateCatalogStore,
    CandidateCatalogUpsertRequest,
)
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine


@pytest.mark.asyncio
async def test_catalog_persists_items_and_searches_by_query_only(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    engine = CandidateCatalogEngine(store, OperationalDecisionEngine(DecisionEngine()))
    try:
        store.create_catalog("p1", CandidateCatalogCreate(catalog_id="products", name="Products"))
        mutation = store.upsert_items(
            "p1",
            "products",
            CandidateCatalogUpsertRequest(
                items=[
                    {"id": "billing", "text": "請求書 支払い クレジットカード", "metadata": {"region": "jp"}},
                    {"id": "checkin", "text": "新宿駅 宿泊施設 チェックイン 入室 方法", "metadata": {"region": "jp"}},
                    {"id": "wifi", "text": "WiFi パスワード インターネット 接続", "metadata": {"region": "jp"}},
                ]
            ),
        )
        assert mutation.item_count == 3

        result = await engine.search(
            "p1",
            "products",
            CandidateCatalogSearchRequest(
                query="新宿駅の近くでチェックイン方法を知りたい",
                top_k=2,
                min_selection_score=0.01,
                min_selection_margin=0.0,
            ),
        )
        assert result.selected_id == "checkin"
        assert result.results[0].id == "checkin"
        assert result.items_indexed == 3
        assert result.results[0].text is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_catalog_metadata_filter_and_upsert_replaces_index(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    engine = CandidateCatalogEngine(store, OperationalDecisionEngine(DecisionEngine()))
    try:
        store.create_catalog("p1", CandidateCatalogCreate(catalog_id="support", name="Support"))
        store.upsert_items(
            "p1",
            "support",
            CandidateCatalogUpsertRequest(
                items=[
                    {"id": "jp", "text": "返金 キャンセル", "metadata": {"region": "jp"}},
                    {"id": "us", "text": "返金 refund cancellation", "metadata": {"region": "us"}},
                ]
            ),
        )
        filtered = await engine.search(
            "p1",
            "support",
            CandidateCatalogSearchRequest(
                query="返金",
                metadata_equals={"region": "jp"},
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert filtered.selected_id == "jp"
        assert all(item.metadata["region"] == "jp" for item in filtered.results)

        store.upsert_items(
            "p1",
            "support",
            CandidateCatalogUpsertRequest(
                items=[{"id": "jp", "text": "駐車場 アクセス", "metadata": {"region": "jp"}}]
            ),
        )
        replaced = await engine.search(
            "p1",
            "support",
            CandidateCatalogSearchRequest(
                query="返金",
                metadata_equals={"region": "jp"},
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert replaced.selected_id is None or replaced.selected_id != "jp"
    finally:
        store.close()


def test_catalog_project_isolation_and_deletion(tmp_path):
    store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    try:
        store.create_catalog("project_a", CandidateCatalogCreate(catalog_id="items", name="A"))
        store.upsert_items(
            "project_a",
            "items",
            CandidateCatalogUpsertRequest(items=[{"id": "x", "text": "alpha"}]),
        )
        with pytest.raises(FileNotFoundError):
            store.get_catalog("project_b", "items")
        with pytest.raises(FileNotFoundError):
            store.delete_items(
                "project_b",
                "items",
                CandidateCatalogDeleteItemsRequest(ids=["x"]),
            )
        assert store.delete_catalog("project_a", "items") is True
        with pytest.raises(FileNotFoundError):
            store.get_catalog("project_a", "items")
    finally:
        store.close()


def test_candidate_catalog_routes_are_wired(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_CANDIDATE_CATALOG_DB", str(tmp_path / "api.sqlite3"))
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/project/candidate-catalogs" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/items" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/search" in paths
