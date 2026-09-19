# Immutable Atomic Catalog Generations

ROOOMTECH Decision Core v0.21 adds immutable candidate-index generations, shared object storage, and an atomic production pointer. The goal is to build a complete replacement index away from production traffic, verify the immutable artifact, prewarm it on the serving node, and then change one small control-plane pointer instead of mutating a live index in place.

This design uses generic immutable-artifact and blue/green deployment patterns. It does not depend on another decision product's API, outputs, prompts, model weights, private schemas, or benchmark data.

## Production search behavior

The existing endpoint remains the production read path:

```text
POST /v1/project/candidate-catalogs/{catalog_id}/search
```

If no atomic generation is active, RTDC uses the mutable reference catalog exactly as before. Once an immutable generation is activated, the same endpoint resolves the active generation and searches that immutable index. A generation is never exposed while it is still being built.

## Generation API

```text
POST /v1/project/candidate-catalogs/{catalog_id}/generations
GET  /v1/project/candidate-catalogs/{catalog_id}/generations
GET  /v1/project/candidate-catalogs/{catalog_id}/generations/{generation_id}
GET  /v1/project/candidate-catalogs/{catalog_id}/active-generation
POST /v1/project/candidate-catalogs/{catalog_id}/generations/{generation_id}/activate
POST /v1/project/candidate-catalogs/{catalog_id}/rollback
```

Generation creation accepts CSV, JSONL, or NDJSON as multipart upload. The request persists the source into the configured shared object store and returns `202 Accepted`; a durable worker then builds the immutable SQLite index in the background. The default safety ceiling is one million logical rows and one GiB of source data. Those are input limits, not throughput guarantees.

## Build lifecycle

```text
queued -> building -> ready -> active -> retired
                  \-> failed
```

A generation has its own immutable source digest, artifact digest, item count, feature-row count, database size, and build duration. `auto_activate=true` is the default. The worker uploads the completed database, verifies/materializes it locally, and only then flips the production pointer.

## Atomic switch

Activation uses a short `BEGIN IMMEDIATE` control-plane transaction. The current generation remains readable during the complete build of the replacement. Only after the new artifact is complete and prewarmed does RTDC update the active-generation row. Readers therefore resolve either the old complete generation or the new complete generation; they do not observe a partially rebuilt sparse index.

`swap_latency_ms` reports the duration of this pointer transaction after prewarming. It deliberately excludes source parsing, index construction, network upload, and first-time artifact download.

Rollback changes the pointer back to the immediately previous generation. Older retired generations can also be activated explicitly by generation ID.

## Shared storage

The default backend is a local directory:

```text
RTDC_SHARED_STORAGE_URL=data/shared-storage
```

For S3-compatible shared storage:

```text
pip install -e '.[s3]'
RTDC_SHARED_STORAGE_URL=s3://my-rtdc-bucket/prod
RTDC_SHARED_CACHE_DIR=data/shared-cache
RTDC_S3_REGION=ap-northeast-1
```

Optional settings:

```text
RTDC_S3_ENDPOINT_URL=
RTDC_S3_SSE=
RTDC_S3_KMS_KEY_ID=
```

The endpoint setting allows S3-compatible services such as MinIO or compatible gateways. Source and index artifacts are addressed by generated keys rather than trusting client filenames as paths. SHA-256 is verified when an immutable artifact is materialized.

For a multi-node SaaS deployment, S3-compatible object storage solves artifact sharing, not control-plane consensus by itself. The reference pointer database is SQLite. A production multi-instance deployment should place control metadata in a shared transactional database or equivalent strongly consistent service before claiming cross-node instantaneous activation.

## Immutable index format

Each generation stores:

- candidate ID, text, and metadata,
- sparse Unicode character n-gram features,
- fixed feature dimension and n-gram configuration,
- a feature lookup index created only after bulk ingestion completes.

The build database uses relaxed SQLite durability settings only while constructing an unpublished temporary artifact. If that process crashes, the generation is rebuilt; no production pointer references the incomplete file. The finished artifact is then treated as immutable.

## One-million-row benchmark

The repository includes:

```text
scripts/benchmark_million_atomic_catalog.py
.github/workflows/benchmark-million.yml
```

The workflow builds a real sparse index over 1,000,000 synthetic unique candidates, activates it, performs 100 lookup queries, and records:

- item and feature rows per second,
- source and database size,
- process maximum RSS,
- atomic pointer-swap latency,
- query mean, p50, p95, and p99 latency,
- exact-match accuracy on the deterministic benchmark queries.

The benchmark intentionally labels the corpus as synthetic. It must not be presented as a production-SLO guarantee, and it is not a comparison against any third-party service. Real text length, n-gram settings, disk performance, S3 transfer time, metadata filters, and query overlap can materially change results.

## Privacy and legal boundary

Generation source files and index databases intentionally persist operator-provided candidate content. Encrypt storage, restrict bucket/database access, set lifecycle/retention policies, and review whether the candidate data can lawfully be stored or sent to an optional external reranker.

RTDC's atomic generation feature is developed from general database, search-index, object-storage, and blue/green deployment concepts. Do not use another vendor's hosted outputs, private API behavior, prompts, model weights, or proprietary benchmark corpus to train, tune, calibrate, or validate RTDC without a separate documented right to do so.
