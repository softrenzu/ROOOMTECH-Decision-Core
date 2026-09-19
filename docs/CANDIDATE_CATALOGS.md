# Persistent Candidate Catalogs

ROOOMTECH Decision Core v0.18 adds project-scoped persistent candidate catalogs. The purpose is to index candidates once, then perform later decisions by sending only a query and optional metadata filters instead of retransmitting the full candidate set on every request.

This feature is independently designed and uses RTDC's own sparse Unicode character n-gram indexing. It does not reproduce a third-party SDK, private protocol, benchmark corpus or proprietary output behavior.

## API

```text
POST   /v1/project/candidate-catalogs
GET    /v1/project/candidate-catalogs
GET    /v1/project/candidate-catalogs/{catalog_id}
DELETE /v1/project/candidate-catalogs/{catalog_id}
PUT    /v1/project/candidate-catalogs/{catalog_id}/items
POST   /v1/project/candidate-catalogs/{catalog_id}/items/delete
POST   /v1/project/candidate-catalogs/{catalog_id}/search
```

Enterprise deployments use a project key with the `candidates` scope. Outside enterprise mode, configure `RTDC_CANDIDATE_API_KEY` or use the admin key.

## Create a catalog

```json
{
  "catalog_id": "hotel_support",
  "name": "Hotel support knowledge",
  "feature_dim": 32768,
  "ngram_min": 2,
  "ngram_max": 4
}
```

The feature configuration is fixed per catalog so all stored items and future queries use the same index representation.

## Upsert items once

```json
{
  "items": [
    {
      "id": "checkin",
      "text": "新宿駅 宿泊施設 チェックイン 入室 方法",
      "metadata": {"region": "jp", "category": "arrival"}
    },
    {
      "id": "wifi",
      "text": "WiFi パスワード インターネット 接続",
      "metadata": {"region": "jp", "category": "equipment"}
    }
  ]
}
```

Upserting an existing item ID replaces its text, metadata and sparse index entries atomically within the SQLite transaction. Each request accepts up to 5,000 items and enforces a total text-character budget.

## Search later with only a query

```json
{
  "query": "チェックイン方法を知りたい",
  "metadata_equals": {"region": "jp"},
  "top_k": 10,
  "final_method": "local_sparse",
  "min_selection_score": 0.15,
  "min_selection_margin": 0.02
}
```

The client no longer needs to send the candidate list. RTDC hashes the query, looks up only indexed items sharing sparse features, applies exact metadata filters, ranks the matches and returns `selected_id`, the top results, `selection_signal`, score margin and `requires_review`.

By default result text is omitted. Set `include_text=true` only when the caller needs the stored text returned.

## Optional bounded reranking

Set `final_method` to `openai_compatible` to send only a bounded shortlist to the configured model provider. `external_rerank_k` is capped at 50. The local inverted index still performs first-stage candidate reduction.

When external reranking is enabled, the query and the bounded shortlist text leave the local process and are subject to the configured provider's privacy and licensing terms.

## Storage and indexing

The reference implementation uses SQLite with three logical structures:

- catalog metadata,
- persisted candidate text and metadata,
- an inverted feature table keyed by hashed Unicode character n-gram feature IDs.

The default database is `data/candidate_catalogs.sqlite3` and can be changed with `RTDC_CANDIDATE_CATALOG_DB`.

Unlike the request-only Dynamic Candidate API, catalog text is intentionally persisted because persistence is the point of the feature. Treat the database as application data: protect it with normal storage encryption, backups, access control and retention policy appropriate to the deployment.

## Tenant boundary

Every catalog, item and feature row is keyed by `project_id`. A catalog created by one enterprise project is returned as not-found to another project. This is an application authorization boundary in the reference SQLite implementation; production multi-tenant SaaS deployments should additionally use storage-layer controls, encrypted volumes and operational isolation appropriate to their risk model.

## Scale boundary

The persistent index removes the need to recompute and resend every candidate on every decision. Query work is driven by sparse feature overlap rather than a full text re-encoding pass over the whole catalog.

SQLite is the reference implementation, not a claim of unlimited scale. For very large catalogs, high write concurrency, or multi-region deployment, the same API can be backed by a dedicated inverted-index or vector/search service while retaining RTDC's project authorization, bounded reranking and review-gating semantics.

## Calibration

`selection_signal` is a routing heuristic, not a calibrated probability of correctness. Before fully automatic production decisions, calibrate `min_selection_score` and `min_selection_margin` on held-out examples owned or authorized by the operator.
