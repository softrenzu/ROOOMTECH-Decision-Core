# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal decision platform for typed, probabilistic, machine-usable decisions.

Version 0.13 adds a project authorization boundary for persisted datasets, human-review items and local models, plus project-authenticated WebSocket traffic. It builds on the enterprise projects/scoped-key control plane, Guardrail Gateway, governed datasets, active learning, calibration and human review. Guardrail results are decision-support signals, not security, compliance, or factuality guarantees. The default fast-profile target remains 150 ms; it is a performance target, not a guaranteed latency claim.

## Main capabilities

- Classification, detection, routing, scoring and verification
- Ranking/search and probabilistic feature extraction
- Arbitrary JSON Schema structured extraction with validation
- Local Map/Reduce over up to 100,000 items
- Optional Redis-sharded distributed Map/Reduce workers
- Streaming Map/Reduce results over NDJSON
- WebSocket realtime decisions and HTTP NDJSON event streaming
- Prevalidated realtime fast profiles using rules, local classifiers, local n-gram ranking, or heuristic extraction
- Bounded realtime worker slots and queue capacity with overload backpressure
- Optional Redis-backed fast-profile sharing across multiple Uvicorn/Gunicorn processes
- Accelerator-aware local inference: low-overhead CPU direct execution and CUDA micro-batching
- Bounded GPU inference queue to avoid unlimited concurrent kernel launches and memory pressure
- Per-request queue, execution and total latency reporting (`queue_ms`, `execution_ms`, `latency_ms`)
- Realtime burst benchmark with p50/p95/p99, throughput and target-hit rate
- Text, image, PDF and audio input
- Trainable local multilingual classifier with CPU/CUDA support
- Confidence, margin, entropy, abstention and human-review gates
- Decision Studio for configuring and testing governed decisions
- Calibration Studio with ECE, Brier score, reliability bins and threshold/coverage curves
- Human Review Queue with retention controls
- Governed datasets with provenance, rights attestation and explicit train/validation/test splits
- Human-review-to-dataset active-learning workflow
- Dataset fingerprints and model lineage for reproducibility and auditability
- Guardrail Gateway for input/output/tool-call policy screening
- Tool Call Gate with allowlists, blocklists, policy checks and explicit authorization for risky actions
- RAG citation/context screening with claim-level pass/review/fail and context injection checks
- Enterprise projects, scoped API keys, daily quotas and metadata-only audit logging
- Project model-promotion records and rollback history
- Project ownership checks for datasets, reviews and local models
- Project-authenticated WebSocket messages with per-message quota/revocation checks
- Personal-use-free / business-use-paid licensing

## Decision Studio

Start the API and open:

```text
http://localhost:8000/studio
```

The Studio is a ROOOMTECH-designed interface. It does not reproduce a third-party console or playground. It can configure and run a decision, route uncertain results to review, calculate thresholds from held-out outcomes, and resolve human-review items.

The governed-dataset interface is available at:

```text
http://localhost:8000/datasets
```

Set `RTDC_STUDIO_API_KEY` in `.env` for protected deployments. When it is blank, `RTDC_ADMIN_API_KEY` is used as the fallback. In enterprise project-key mode, management APIs fail closed if the required admin/studio secret is not configured.

## Enterprise control plane and tenant isolation

The enterprise reference control plane is disabled by default. Enable project-key enforcement with:

```bash
RTDC_ADMIN_API_KEY=<admin-secret>
RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS=true
RTDC_ENTERPRISE_KEY_PEPPER=<stable-secret-pepper>
```

Administrative APIs:

```text
POST   /v1/admin/projects
GET    /v1/admin/projects
GET    /v1/admin/projects/{project_id}
PATCH  /v1/admin/projects/{project_id}
POST   /v1/admin/projects/{project_id}/keys
GET    /v1/admin/projects/{project_id}/keys
DELETE /v1/admin/projects/{project_id}/keys/{key_id}
GET    /v1/admin/audit
POST   /v1/admin/projects/{project_id}/models/promote
GET    /v1/admin/projects/{project_id}/models
POST   /v1/admin/projects/{project_id}/models/rollback
GET    /v1/project/deployments
POST   /v1/project/predict
```

Project keys are high-entropy credentials returned only once. The local control-plane database stores only a digest; an optional HMAC pepper can be configured separately. When enforcement is enabled, ordinary HTTP inference routes require `X-RTDC-Project-Key` with the appropriate scope. Daily project quotas return HTTP 429 after exhaustion.

Persisted tenant resources are registered to one project. Cross-project lookups for datasets, reviews and models are returned as not found rather than revealing the owner. Dataset training automatically registers the resulting model to the same project. Review-to-dataset import only considers reviews owned by that project. General local-classifier decisions carrying an explicit `model_id` inherit the authenticated tenant context and cannot invoke another project's registered model.

Project-scoped dataset APIs:

```text
POST   /v1/project/datasets
GET    /v1/project/datasets
GET    /v1/project/datasets/{dataset_id}
DELETE /v1/project/datasets/{dataset_id}
POST   /v1/project/datasets/{dataset_id}/examples
GET    /v1/project/datasets/{dataset_id}/examples
POST   /v1/project/datasets/{dataset_id}/import-reviews
POST   /v1/project/datasets/{dataset_id}/train
GET    /v1/project/datasets/{dataset_id}/models
```

Project-scoped review and active-learning APIs:

```text
POST /v1/project/reviews
GET  /v1/project/reviews
GET  /v1/project/reviews/{review_id}
POST /v1/project/reviews/{review_id}/resolve
GET  /v1/project/reviews/export/training-examples
GET  /v1/project/active-learning/candidates
```

Project-scoped model APIs:

```text
GET  /v1/project/models
GET  /v1/project/models/{model_id}
POST /v1/project/models/{model_id}/predict
```

Authenticated project inference requests create audit metadata containing project/key IDs, method, path, status, latency and request ID. Request bodies, prompts and outputs are deliberately not copied into the audit table.

When enterprise enforcement is enabled, `/v1/realtime/ws` uses `X-RTDC-Project-Key` with the `realtime` scope. The key is checked at connection time and before every message, so revocation, expiry, project disablement and quota exhaustion affect long-lived connections. Each processed message receives the project tenant context and creates metadata-only audit information.

The v0.13 tenant boundary is an application authorization boundary between project credentials. The reference SQLite dataset/review/model stores remain shared rather than database-per-tenant. Production SaaS deployments should add managed encrypted storage and database-level tenant controls for defense in depth. Legacy realtime fast profiles are not yet tenant-owned objects and should not be treated as project-private without additional profile ownership or edge isolation.

See `docs/ENTERPRISE_CONTROL_PLANE.md` and `docs/TENANT_ISOLATION.md`.

## Guardrail Gateway

```text
POST /v1/guardrails/evaluate
POST /v1/guardrails/tool-call
POST /v1/guardrails/rag
```

`/v1/guardrails/evaluate` can screen AI input, model output, proposed tool calls, or combinations of them. Built-in local signals currently cover prompt-injection-like instruction patterns, sensitive-data-like patterns, credential-like strings and risky-action words. Operators can add their own keyword and semantic policy rules.

`/v1/guardrails/tool-call` is intended to run before an agent executes an action. It supports tool allowlists/blocklists and can block risky actions when explicit authorization is missing. Applications must still enforce normal authentication, authorization, transaction limits, idempotency and approval controls.

`/v1/guardrails/rag` checks each explicit claim against only its cited passages using a fast local lexical-support signal and checks retrieved context for prompt-injection-like patterns. Any non-pass claim or detected context injection is routed to review. Lexical support is a first-pass screening signal and does not prove entailment, factual correctness, absence of contradiction or source authority.

Set `RTDC_GUARDRAIL_API_KEY` to protect these endpoints outside enterprise project-key mode. Under enterprise enforcement, a blank dedicated guardrail key lets the project `guardrails` scope provide the primary gateway credential; an explicit dedicated guardrail key can still be configured for defense in depth. The guardrail module does not persist submitted input, output, citations or tool arguments by default.

See `docs/GUARDRAILS.md`.

## Governed datasets and active learning

Operator/admin dataset APIs remain available for maintenance and single-operator deployments:

```text
POST /v1/datasets
GET  /v1/datasets
GET  /v1/datasets/{dataset_id}
POST /v1/datasets/{dataset_id}/examples
GET  /v1/datasets/{dataset_id}/examples
POST /v1/datasets/{dataset_id}/import-reviews
POST /v1/datasets/{dataset_id}/train
GET  /v1/datasets/{dataset_id}/models
GET  /v1/active-learning/candidates
POST /v1/datasets/purge-expired
```

Dataset creation requires a provenance statement plus `rights_attested: true`. Allowed source categories are operator-owned, consented, licensed, synthetic, internal business records and public-domain data. The workflow does not require outputs from another proprietary decision service.

Examples are assigned to explicit `train`, `validation` or `test` splits. Duplicate raw text cannot be inserted into another split within the same dataset, reducing accidental train/test leakage. `GET /v1/datasets/{dataset_id}` returns a deterministic SHA-256 fingerprint over the logical dataset state.

Training uses only the `train` split. A separate validation or test split can be benchmarked after training. Each training run creates a lineage record containing the resulting model ID, dataset fingerprint, training metadata and held-out evaluation metrics.

Resolved human-review items can be imported into a dataset only when their raw input was explicitly retained. Pending review items are exposed as uncertainty-prioritized active-learning candidates; a model suggestion is never automatically treated as ground truth.

See `docs/DATASET_GOVERNANCE.md`.

## Governed decisions

Use `POST /v1/decide/governed` to combine a normal decision with a review policy:

```json
{
  "decision": {
    "input": "ログインできません",
    "provider": "rules",
    "decisions": [
      {
        "id": "support_route",
        "question": "Which support route should handle this request?",
        "choices": ["account", "billing", "other"],
        "keywords": {"account": ["ログイン", "パスワード"], "billing": ["請求"]}
      }
    ]
  },
  "policy": {
    "review_below_confidence": 0.80,
    "review_if_requires_review": true,
    "review_if_abstained": true,
    "store_input": false,
    "retention_days": 30
  }
}
```

`store_input` defaults to `false`. When false, raw input is not persisted in the review database; a SHA-256 digest is retained for correlation. Enable raw-input storage only when the operator has an appropriate legal basis, security controls and retention policy.

## Calibration Studio API

`POST /v1/evals/calibrate` consumes held-out outcomes owned by the operator and reports accuracy, mean confidence, expected calibration error, Brier score, reliability bins, a threshold/coverage curve and the widest-coverage observed threshold satisfying the requested empirical error rate and minimum sample count.

A recommended threshold is an empirical result for the supplied held-out data, not a universal accuracy guarantee. Re-evaluate it when the model, dataset, domain or traffic distribution changes.

## Human review API

Operator/admin review APIs:

```text
POST /v1/reviews
GET  /v1/reviews
GET  /v1/reviews/{review_id}
POST /v1/reviews/{review_id}/resolve
POST /v1/reviews/purge-expired
GET  /v1/reviews/export/training-examples
```

The built-in SQLite review, dataset and enterprise stores are intended for local, evaluation and single-node deployments. Enterprise/multi-node deployments should use approved managed storage with organizational access controls, encryption, backups, audit logging and retention enforcement.

## Fast realtime path

Create a profile once so request validation and routing configuration are not rebuilt for every event:

```text
POST   /v1/realtime/profiles
GET    /v1/realtime/profiles
DELETE /v1/realtime/profiles/{profile_id}
POST   /v1/realtime/fast
```

Example profile:

```json
{
  "profile_id": "support-route",
  "kind": "route",
  "target_ms": 150,
  "request": {
    "provider": "rules",
    "routes": [
      {"id": "billing", "keywords": ["請求", "invoice"]},
      {"id": "account", "keywords": ["ログイン", "password"]}
    ]
  }
}
```

Fast profiles intentionally reject `auto` and `openai_compatible` providers because those can make external network calls and make latency unpredictable. Local classifier models can be prewarmed when the profile is created.

The fast path uses a bounded scheduler. `RTDC_FAST_WORKERS` controls concurrent execution slots, `RTDC_FAST_QUEUE_CAPACITY` caps pending work, and `RTDC_FAST_MAX_QUEUE_WAIT_MS` caps queue waiting. HTTP fast-path requests return 429 when capacity is exhausted or queue wait exceeds the configured limit instead of allowing latency to grow without bound.

For local classifiers, CPU and CUDA use different scheduling strategies. CPU requests use bounded direct execution without an artificial batching wait. CUDA requests are automatically micro-batched and pass through a bounded GPU execution gate. See `.env.example` and `docs/PERFORMANCE.md` for tuning controls.

## Multiple application workers

For CPU-heavy deployments, multiple server processes can be used. Enable the optional Redis registry so dynamically-created fast-profile definitions are visible to every worker:

```bash
pip install -e '.[distributed]'
export RTDC_REDIS_URL=redis://127.0.0.1:6379/0
export RTDC_FAST_PROFILE_REDIS_ENABLED=true
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Each process compiles a shared profile locally on first use. Redis Pub/Sub invalidates stale process-local copies when a shared profile changes or is deleted. Local-classifier model files must also be readable by each process.

## Performance benchmarks

```text
POST /v1/benchmarks/realtime-fast
POST /v1/benchmarks/mapreduce-load
```

The realtime benchmark measures a burst workload against an existing fast profile and reports p50/p95/p99 latency, calls/second, errors and target-hit rate. For transport-inclusive measurements against a deployed server, use `benchmarks/http_realtime_load.py` or `benchmarks/websocket_realtime_load.py`.

See `docs/PERFORMANCE.md` for methodology and interpretation.

## Other endpoints

```text
POST /v1/decide
POST /v1/decide/governed
POST /v1/extract
POST /v1/mapreduce/run
POST /v1/mapreduce/stream
POST /v1/mapreduce/jobs
GET  /v1/mapreduce/jobs/{job_id}
WS   /v1/realtime/ws
POST /v1/realtime/stream
POST /v1/ops/detect
POST /v1/ops/route
POST /v1/ops/score
POST /v1/ops/verify
POST /v1/ops/rank
POST /v1/ops/search
POST /v1/ops/features
POST /v1/multimodal/decide
```

## Quick start

```bash
cp .env.example .env
pip install -e '.[multimodal]'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For Redis Map/Reduce workers:

```bash
pip install -e '.[distributed]'
python -m app.mapreduce_worker
```

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`. Business, professional, organizational or institutional use requires a separate paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

## Independent development

This is an independent product. It does not include third-party proprietary source code, prompts, private APIs, decision-service outputs, copied benchmark data, copied UI assets or copied product documentation, and it is not marketed as a clone or official compatible implementation of another vendor's product.

Development separation rules are documented in `docs/LEGAL_DESIGN.md` and `docs/INDEPENDENT_PRODUCT_DEVELOPMENT.md`. Dataset-specific controls are documented in `docs/DATASET_GOVERNANCE.md`; guardrail-specific boundaries and limitations are in `docs/GUARDRAILS.md`; enterprise and tenant boundaries are in `docs/ENTERPRISE_CONTROL_PLANE.md` and `docs/TENANT_ISOLATION.md`. These engineering controls reduce avoidable intellectual-property and contractual risk, but they are not a guarantee against claims.

## Version

`0.13.0`
