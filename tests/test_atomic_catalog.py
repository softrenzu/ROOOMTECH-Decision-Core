import json
from pathlib import Path

import pytest

from app.atomic_catalog import (
    AtomicCatalogControlStore,
    AtomicCatalogRuntime,
    ImmutableCatalogBuilder,
)
from app.candidate_catalog import CandidateCatalogSearchRequest
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine
from app.shared_object_store import LocalObjectStore


def _write_jsonl(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _ready_generation(control, storage, builder, tmp_path, catalog_id, rows):
    source = tmp_path / f"{catalog_id}-{len(control.list_generations('p1', catalog_id))}.jsonl"
    _write_jsonl(source, rows)
    source_ref = storage.put_file(source, f"sources/{source.name}")
    generation = control.create_generation(
        project_id="p1",
        catalog_id=catalog_id,
        source_uri=source_ref.uri,
        source_sha256=source_ref.sha256,
        source_format="jsonl",
        batch_size=100,
        max_rows=1000,
        feature_dim=4096,
        ngram_min=2,
        ngram_max=3,
        auto_activate=False,
    )
    result = builder.build(
        source_path=source,
        source_format="jsonl",
        generation_id=generation.generation_id,
        batch_size=100,
        max_rows=1000,
        feature_dim=4096,
        ngram_min=2,
        ngram_max=3,
    )
    artifact = storage.put_file(result.database_path, f"indexes/{generation.generation_id}.sqlite3")
    Path(result.database_path).unlink(missing_ok=True)
    return control.mark_ready(
        generation,
        artifact_uri=artifact.uri,
        artifact_sha256=artifact.sha256,
        item_count=result.item_count,
        feature_rows=result.feature_rows,
        database_bytes=result.database_bytes,
        build_seconds=result.build_seconds,
    )


@pytest.mark.asyncio
async def test_atomic_generation_switch_and_rollback(tmp_path):
    control = AtomicCatalogControlStore(str(tmp_path / "control.sqlite3"))
    storage = LocalObjectStore(tmp_path / "objects")
    builder = ImmutableCatalogBuilder(tmp_path / "build")
    runtime = AtomicCatalogRuntime(control, storage, OperationalDecisionEngine(DecisionEngine()))
    try:
        g1 = _ready_generation(
            control,
            storage,
            builder,
            tmp_path,
            "products",
            [
                {"id": "alpha", "text": "alpha mountain jacket", "metadata": {"region": "jp"}},
                {"id": "other", "text": "billing invoice payment", "metadata": {"region": "jp"}},
            ],
        )
        first = control.activate("p1", "products", g1.generation_id)
        assert first.active.generation_id == g1.generation_id

        result = await runtime.search(
            "p1",
            "products",
            CandidateCatalogSearchRequest(
                query="alpha jacket",
                metadata_equals={"region": "jp"},
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert result.selected_id == "alpha"
        assert result.index_method == "immutable_atomic_sparse_unicode_char_ngram"

        g2 = _ready_generation(
            control,
            storage,
            builder,
            tmp_path,
            "products",
            [
                {"id": "beta", "text": "beta beach sandals", "metadata": {"region": "jp"}},
                {"id": "other2", "text": "parking access map", "metadata": {"region": "jp"}},
            ],
        )
        second = control.activate("p1", "products", g2.generation_id)
        assert second.previous_generation_id == g1.generation_id
        result2 = await runtime.search(
            "p1",
            "products",
            CandidateCatalogSearchRequest(
                query="beta sandals",
                metadata_equals={"region": "jp"},
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert result2.selected_id == "beta"

        rolled = control.rollback("p1", "products")
        assert rolled.active.generation_id == g1.generation_id
        result3 = await runtime.search(
            "p1",
            "products",
            CandidateCatalogSearchRequest(
                query="alpha mountain",
                min_selection_score=0.0,
                min_selection_margin=0.0,
            ),
        )
        assert result3.selected_id == "alpha"
    finally:
        runtime.close()
        control.close()


def test_atomic_generation_is_project_scoped(tmp_path):
    control = AtomicCatalogControlStore(str(tmp_path / "control.sqlite3"))
    try:
        generation = control.create_generation(
            project_id="project_a",
            catalog_id="items",
            source_uri=(tmp_path / "source.jsonl").resolve().as_uri(),
            source_sha256="0" * 64,
            source_format="jsonl",
            batch_size=100,
            max_rows=100,
            feature_dim=4096,
            ngram_min=2,
            ngram_max=3,
            auto_activate=False,
        )
        with pytest.raises(FileNotFoundError):
            control.get_generation("project_b", "items", generation.generation_id)
        assert control.get_active("project_b", "items") is None
    finally:
        control.close()


def test_atomic_catalog_routes_are_wired():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/project/candidate-catalogs/{catalog_id}/generations" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/generations/{generation_id}/activate" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/rollback" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/active-generation" in paths
