# Bulk Candidate Imports

ROOOMTECH Decision Core v0.20 adds durable asynchronous CSV/JSONL imports for persistent candidate catalogs. The control plane is designed for feeds in the 100,000 to 1,000,000 row range without loading the full source file into application memory.

The default configured hard limit is 1,000,000 logical rows and 1 GiB per uploaded source file. These are safety limits, not a throughput guarantee. Large-production deployments should benchmark their own storage, CPU and catalog characteristics before setting operational SLOs.

## API

```text
POST /v1/project/candidate-catalogs/{catalog_id}/imports
GET  /v1/project/candidate-catalogs/{catalog_id}/imports
GET  /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}
GET  /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/failures
POST /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/pause
POST /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/resume
POST /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/cancel
POST /v1/project/candidate-catalogs/{catalog_id}/imports/{job_id}/retry-failed
```

Enterprise mode requires a project key with the `candidates` scope. Job lookup is project-scoped; a job ID from another project is returned as not found.

## Upload

The create endpoint accepts `multipart/form-data`:

- `file`: CSV, JSONL or NDJSON source.
- `format`: `auto`, `csv` or `jsonl`.
- `batch_size`: 1 to 5,000, default 1,000.
- `max_rows`: per-job row guard, default 1,000,000 and bounded by `RTDC_CANDIDATE_IMPORT_MAX_ROWS`.
- `on_error`: `continue` or `stop`.
- `snapshot_before_import`: defaults to `true`.

The HTTP request only persists the uploaded source and durable job metadata. Candidate indexing runs asynchronously after the request returns `202 Accepted`.

The server calculates and records a SHA-256 digest of the source. The original client filename is metadata only; the server uses a generated source path and never trusts the filename as a filesystem path.

## JSONL format

Each non-empty line is one candidate object:

```json
{"id":"sku-1001","text":"waterproof hiking jacket","metadata":{"region":"jp","category":"outerwear"}}
```

`id` and `text` are required. `metadata` is optional and must be an object.

## CSV format

CSV requires `id` and `text` columns. Optional metadata can be supplied as JSON in `metadata_json` (or `metadata`) and simple columns prefixed with `meta_` are merged into metadata.

```csv
id,text,metadata_json,meta_language
sku-1001,waterproof hiking jacket,"{""region"":""jp""}",en
```

## Durable job states

Jobs move through:

```text
queued -> running -> completed
                  -> completed_with_errors
                  -> failed
queued/running -> paused -> queued
queued/running/paused -> cancelled
```

Job state is stored in SQLite separately from the uploaded source. Worker claims use a lease. If a worker process dies, another worker can reclaim a running job after its lease expires and resume from the last durable checkpoint. Candidate writes are upserts, so replaying a small checkpoint window is safe for item identity.

Only one active leased import for the same project/catalog is claimed at a time. This avoids two large feeds interleaving writes into the same catalog through the reference worker control plane.

## Progress

The job returns:

- `cursor_row`,
- `processed_rows`,
- `succeeded_rows`,
- `failed_rows`,
- `retryable_failed_rows`,
- `total_rows` when the source has reached a terminal scan,
- `progress` when a total is known.

While a previously unseen streaming source is still being read, `total_rows` can be null. `processed_rows` remains available as monotonic operational progress.

## Pause, resume and cancel

Pause is cooperative and takes effect at a batch/checkpoint boundary. Resume requeues the same durable job. Source files are retained so paused jobs and crash recovery can continue later.

Cancel stops future batches but does not automatically undo batches already committed. With the default `snapshot_before_import=true`, the job records a pre-import catalog `snapshot_id`; operators can explicitly restore that snapshot when they want to roll back a partially applied or cancelled feed.

## Failure handling and retry

Parse/validation failures are recorded with row number, bounded preview and error text. They are marked non-retryable because replaying the same malformed row cannot repair it.

Transient batch-write failures keep the validated candidate payload and are marked retryable. `retry-failed` creates a child job containing only retryable failed payloads; successfully imported rows are not deliberately scheduled again.

A retry is a new auditable job with `retry_of_job_id`, rather than rewriting the history of the original job.

## Snapshot safety

By default the worker creates a catalog snapshot immediately before processing the first batch. This gives large feed refreshes an explicit rollback point. The snapshot is application-level protection, not a substitute for database backups.

For very large catalogs, snapshots can be storage-intensive. Operators can turn them off per job only when they have an alternative rollback strategy.

## Resource limits

```text
RTDC_CANDIDATE_IMPORT_DB=data/candidate_imports.sqlite3
RTDC_CANDIDATE_IMPORT_DIR=data/candidate_imports
RTDC_CANDIDATE_IMPORT_MAX_ROWS=1000000
RTDC_CANDIDATE_IMPORT_MAX_BYTES=1073741824
RTDC_CANDIDATE_IMPORT_POLL_SECONDS=1
```

The worker streams source rows and only keeps the current candidate batch in memory during a normal import. The catalog itself remains backed by RTDC's persistent sparse index.

SQLite is the reference control plane. Sustained multi-region or very high write-throughput deployments should move job scheduling/source storage/index storage to production-grade shared services while preserving the same project authorization, checkpoint, lease, snapshot and audit semantics.

## Privacy and legal boundary

Bulk import source files and candidate text are intentionally persisted because the operator requested indexing. Protect the import directory and catalog database with storage encryption, access control, backup and retention policies appropriate to the data.

This feature does not require outputs, prompts, schemas, SDK internals or training data from any third-party decision product. It implements generic asynchronous ingestion, checkpointing and indexing patterns with RTDC-owned code and data models.
