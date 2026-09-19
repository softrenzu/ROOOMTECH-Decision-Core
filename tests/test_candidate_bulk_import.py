import hashlib
import json

from app.candidate_bulk_import import CandidateBulkImportStore, CandidateBulkImportWorker
from app.candidate_catalog import CandidateCatalogCreate, CandidateCatalogSearchRequest, CandidateCatalogStore
from app.candidate_catalog_ops import CandidateCatalogSnapshotManager


def _new_services(tmp_path):
    catalog_store = CandidateCatalogStore(str(tmp_path / "catalog.sqlite3"))
    jobs = CandidateBulkImportStore(
        str(tmp_path / "imports.sqlite3"),
        str(tmp_path / "uploads"),
    )
    snapshots = CandidateCatalogSnapshotManager(catalog_store)
    worker = CandidateBulkImportWorker(jobs, catalog_store, snapshots)
    return catalog_store, jobs, snapshots, worker


def test_jsonl_bulk_import_persists_progress_failures_and_snapshot(tmp_path):
    catalog_store, jobs, snapshots, worker = _new_services(tmp_path)
    try:
        catalog_store.create_catalog("p1", CandidateCatalogCreate(catalog_id="products", name="Products"))
        source = tmp_path / "items.jsonl"
        source.write_text(
            "\n".join(
                [
                    json.dumps({"id": "checkin", "text": "新宿 チェックイン 入室", "metadata": {"region": "jp"}}, ensure_ascii=False),
                    "{not-json}",
                    json.dumps({"id": "wifi", "text": "WiFi パスワード 接続", "metadata": {"region": "jp"}}, ensure_ascii=False),
                ]
            ),
            encoding="utf-8",
        )
        raw = source.read_bytes()
        job = jobs.create_job(
            project_id="p1",
            catalog_id="products",
            source_path=str(source),
            source_format="jsonl",
            original_filename="items.jsonl",
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            batch_size=2,
            max_rows=1_000_000,
            on_error="continue",
            snapshot_before_import=True,
        )
        assert jobs.claim_next(worker.worker_id) == job.job_id
        worker.process_job(job.job_id)

        done = jobs.get_job("p1", job.job_id)
        assert done.status == "completed_with_errors"
        assert done.total_rows == 3
        assert done.processed_rows == 3
        assert done.succeeded_rows == 2
        assert done.failed_rows == 1
        assert done.snapshot_id is not None
        assert done.progress == 1.0
        failures = jobs.list_failures("p1", job.job_id)
        assert len(failures) == 1
        assert failures[0].retryable is False
        assert catalog_store.get_catalog("p1", "products").item_count == 2
    finally:
        catalog_store.close()
        jobs.close()


def test_csv_bulk_import_supports_metadata_columns(tmp_path):
    catalog_store, jobs, snapshots, worker = _new_services(tmp_path)
    try:
        catalog_store.create_catalog("p1", CandidateCatalogCreate(catalog_id="support", name="Support"))
        source = tmp_path / "items.csv"
        source.write_text(
            'id,text,metadata_json,meta_language\n'
            'a,"refund cancellation","{\"\"region\"\":\"\"us\"\"}",en\n'
            'b,"返金 キャンセル","{\"\"region\"\":\"\"jp\"\"}",ja\n',
            encoding="utf-8",
        )
        raw = source.read_bytes()
        job = jobs.create_job(
            project_id="p1",
            catalog_id="support",
            source_path=str(source),
            source_format="csv",
            original_filename="items.csv",
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            batch_size=100,
            max_rows=1_000_000,
            on_error="stop",
            snapshot_before_import=False,
        )
        assert jobs.claim_next(worker.worker_id) == job.job_id
        worker.process_job(job.job_id)
        done = jobs.get_job("p1", job.job_id)
        assert done.status == "completed"
        assert done.succeeded_rows == 2
        assert catalog_store.get_catalog("p1", "support").item_count == 2
    finally:
        catalog_store.close()
        jobs.close()


def test_pause_resume_cancel_and_project_isolation(tmp_path):
    catalog_store, jobs, snapshots, worker = _new_services(tmp_path)
    try:
        catalog_store.create_catalog("p1", CandidateCatalogCreate(catalog_id="items", name="Items"))
        source = tmp_path / "items.jsonl"
        source.write_text(json.dumps({"id": "x", "text": "alpha"}), encoding="utf-8")
        raw = source.read_bytes()
        job = jobs.create_job(
            project_id="p1",
            catalog_id="items",
            source_path=str(source),
            source_format="jsonl",
            original_filename="items.jsonl",
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            batch_size=1,
            max_rows=100,
            on_error="continue",
            snapshot_before_import=False,
        )
        assert jobs.pause("p1", job.job_id).status == "paused"
        assert jobs.resume("p1", job.job_id).status == "queued"
        assert jobs.cancel("p1", job.job_id).status == "cancelled"
        try:
            jobs.get_job("p2", job.job_id)
            assert False, "cross-project job lookup should not succeed"
        except FileNotFoundError:
            pass
    finally:
        catalog_store.close()
        jobs.close()


def test_retryable_failed_rows_create_child_job_and_import(tmp_path):
    catalog_store, jobs, snapshots, worker = _new_services(tmp_path)
    try:
        catalog_store.create_catalog("p1", CandidateCatalogCreate(catalog_id="items", name="Items"))
        source = tmp_path / "source.jsonl"
        source.write_text("{}", encoding="utf-8")
        raw = source.read_bytes()
        parent = jobs.create_job(
            project_id="p1",
            catalog_id="items",
            source_path=str(source),
            source_format="jsonl",
            original_filename="source.jsonl",
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_bytes=len(raw),
            batch_size=10,
            max_rows=100,
            on_error="continue",
            snapshot_before_import=False,
        )
        payload = {"id": "retry-me", "text": "再試行できる候補", "metadata": {"kind": "test"}}
        jobs.record_failure(
            parent.job_id,
            7,
            item_id="retry-me",
            error="temporary write failure",
            payload_json=json.dumps(payload, ensure_ascii=False),
            raw_preview=None,
            retryable=True,
        )
        jobs.finish(parent.job_id, total_rows=1, failed_rows=1)
        retry = jobs.create_retry_job("p1", parent.job_id)
        assert retry.retry_of_job_id == parent.job_id
        assert jobs.claim_next(worker.worker_id) == retry.job_id
        worker.process_job(retry.job_id)
        done = jobs.get_job("p1", retry.job_id)
        assert done.status == "completed"
        assert done.succeeded_rows == 1
        assert catalog_store.get_catalog("p1", "items").item_count == 1
    finally:
        catalog_store.close()
        jobs.close()


def test_bulk_import_routes_are_wired():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/pause" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/resume" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/cancel" in paths
    assert "/v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/retry-failed" in paths
