# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal decision platform for typed, probabilistic, machine-usable decisions.

Version 0.9 adds an independently designed Decision Studio, empirical calibration, confidence-threshold recommendations, governed decisions and a privacy-conscious human-review queue. The default fast-profile target remains 150 ms; it is a performance target, not a guaranteed latency claim.

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
- Human Review Queue with retention controls and active-learning export
- Personal-use-free / business-use-paid licensing

## Decision Studio

Start the API and open:

```text
http://localhost:8000/studio
```

The Studio is a ROOOMTECH-designed interface. It does not reproduce a third-party console or playground. It can configure and run a decision, route uncertain results to review, calculate thresholds from held-out outcomes, resolve human-review items, and export operator-approved training examples.

Set `RTDC_STUDIO_API_KEY` in `.env` for protected deployments. When it is blank, `RTDC_ADMIN_API_KEY` is used as the fallback.

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

```text
POST /v1/reviews
GET  /v1/reviews
GET  /v1/reviews/{review_id}
POST /v1/reviews/{review_id}/resolve
POST /v1/reviews/purge-expired
GET  /v1/reviews/export/training-examples
```

The built-in SQLite queue is intended for single-node and evaluation deployments. Enterprise/multi-node deployments should place the review workflow on an approved managed database with organizational access controls, backups and retention enforcement.

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

Development separation rules are documented in `docs/LEGAL_DESIGN.md` and `docs/INDEPENDENT_PRODUCT_DEVELOPMENT.md`. These engineering controls reduce avoidable intellectual-property and contractual risk, but they are not a guarantee against claims. Commercial launch should include trademark and counsel review for the intended markets and claims.

## Version

`0.9.0`
