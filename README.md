# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal decision engine for typed, probabilistic, machine-usable decisions.

Version 0.8 adds bounded realtime scheduling, overload backpressure, accelerator-aware local inference, automatic CUDA micro-batching and explicit queue/execution latency reporting. The default fast-profile target remains 150 ms; it is a performance target, not a guaranteed latency claim. Measure it on the deployment hardware and workload you intend to use.

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
- Accelerator-aware local inference: low-overhead CPU direct execution and CUDA micro-batching
- Bounded GPU inference queue to avoid unlimited concurrent kernel launches and memory pressure
- Per-request queue, execution and total latency reporting (`queue_ms`, `execution_ms`, `latency_ms`)
- Per-request latency-budget reporting (`target_ms`, `within_target`)
- Realtime burst benchmark with p50/p95/p99, throughput and target-hit rate
- Map/Reduce load benchmark with repeated-run throughput and failure counts
- Text, image, PDF and audio input
- Trainable local multilingual classifier with CPU/CUDA support
- Confidence, margin, entropy, abstention and human-review gates
- Personal-use-free / business-use-paid licensing

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

Then send only the changing input:

```json
{
  "profile_id": "support-route",
  "input": "ログインできません",
  "request_id": "req-001"
}
```

Fast profiles intentionally reject `auto` and `openai_compatible` providers because those can make external network calls and make latency unpredictable. Local classifier models can be prewarmed when the profile is created.

WebSocket clients can also send `kind: "fast"` with the same request body.

The fast path uses a bounded scheduler. `RTDC_FAST_WORKERS` controls concurrent execution slots, `RTDC_FAST_QUEUE_CAPACITY` caps pending work, and `RTDC_FAST_MAX_QUEUE_WAIT_MS` caps queue waiting. HTTP fast-path requests return 429 when capacity is exhausted or queue wait exceeds the configured limit instead of allowing latency to grow without bound.

For local classifiers, CPU and CUDA use different scheduling strategies. CPU requests use bounded direct execution without an artificial batching wait. CUDA requests are automatically micro-batched and pass through a bounded GPU execution gate. See `.env.example` and `docs/PERFORMANCE.md` for tuning controls.

## Performance benchmarks

```text
POST /v1/benchmarks/realtime-fast
POST /v1/benchmarks/mapreduce-load
```

The realtime benchmark measures a burst workload against an existing fast profile and reports p50/p95/p99 latency, calls/second, errors and the fraction of requests that met the profile target. Fast responses also separate time spent waiting for an execution slot from actual execution time.

The Map/Reduce benchmark actually executes the supplied job repeatedly and reports run latency, items/second and failures.

For transport-inclusive measurements against a deployed server, use `benchmarks/http_realtime_load.py` or `benchmarks/websocket_realtime_load.py`.

See `docs/PERFORMANCE.md` for methodology and interpretation.

## Other endpoints

```text
POST /v1/decide
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

For Redis workers:

```bash
pip install -e '.[distributed]'
python -m app.mapreduce_worker
```

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`. Business, professional, organizational or institutional use requires a separate paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

## Independence

This is an independent product. It does not include third-party proprietary source code, prompts, private APIs, decision-service outputs, copied benchmark data or third-party UI assets, and it is not marketed as a clone or official compatible implementation of another vendor's product.

## Version

`0.8.0`
