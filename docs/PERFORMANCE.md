# Performance and latency methodology

ROOOMTECH Decision Core 0.7 introduces a latency-sensitive fast path and explicit load benchmarks. The design goal is to make latency measurable and controllable rather than to publish an unconditional speed claim.

## Realtime fast profiles

A fast profile prevalidates a request template and stores it in memory. Each realtime call then supplies only the changing input text. This removes repeated configuration parsing and prevents accidental fallback to a remote model.

Supported network-free profile modes:

- `rules`
- `local_classifier`
- `local_ngram` ranking
- `heuristic` schema extraction

`auto` and `openai_compatible` are rejected for fast profiles because network latency is outside the process and can vary materially.

Profiles are process-local and are not persisted. Recreate them after restart. `RTDC_FAST_PROFILE_LIMIT` controls the maximum in-memory profile count.

## 150 ms target

The default profile target is 150 ms. Every `/v1/realtime/fast` response reports:

- `latency_ms`: measured execution time inside the Decision Core process
- `target_ms`: the configured profile target
- `within_target`: whether the request completed successfully within the target

Do not treat 150 ms as a guarantee. Performance varies with CPU/GPU, container limits, input length, classifier size, concurrent load, Python runtime, operating system, and surrounding network/proxy latency.

For a user-facing claim, benchmark the same build, hardware, deployment region, concurrency and input distribution that will be used in production.

## Realtime load benchmark

`POST /v1/benchmarks/realtime-fast` runs an actual burst workload against an existing fast profile.

Example:

```json
{
  "profile_id": "support-route",
  "inputs": [
    "ログインできません",
    "請求書を再発行してください",
    "パスワードを変更したいです"
  ],
  "repeat_runs": 100,
  "concurrency": 32,
  "warmup_runs": 10
}
```

The response includes:

- total measured calls
- error count
- wall-clock duration
- minimum / mean / p50 / p95 / p99 / maximum latency
- calls per second
- target-hit rate

The benchmark measures queueing plus execution inside the process under the requested burst load. This is useful for capacity planning, but it does not include client-to-server network transit.

## HTTP transport-inclusive benchmark

Use:

```bash
python benchmarks/http_realtime_load.py \
  --base-url http://localhost:8000 \
  --profile-id support-route \
  --input 'ログインできません' \
  --requests 10000 \
  --concurrency 64
```

Add `--api-key` when `RTDC_REALTIME_API_KEY` is configured.

This utility measures round-trip HTTP latency from the benchmark client and therefore includes serialization, ASGI/server handling and network transport between the client and server.

Run the client on a separate machine or container when you want a realistic end-to-end test.

## Map/Reduce load benchmark

`POST /v1/benchmarks/mapreduce-load` repeatedly executes the supplied Map/Reduce request.

Example:

```json
{
  "request": {
    "items": [
      {"text": "refund please"},
      {"text": "where is my order"}
    ],
    "map": {
      "kind": "detect",
      "input_field": "text",
      "config": {
        "property": "refund request",
        "provider": "rules",
        "keywords": ["refund"]
      }
    },
    "reduce": {"kind": "collect"},
    "concurrency": 32,
    "include_map_results": false
  },
  "repeat_runs": 5,
  "warmup_runs": 1
}
```

It reports each run's latency, successful/failed item count and items per second, plus overall p50/p95/p99 run latency and aggregate items per second.

For large tests, set `include_map_results` to `false` unless individual outputs are required. This reduces response size and memory pressure.

## Fair comparisons

When comparing this project with any external product or model, keep these factors constant:

1. identical input dataset and ground-truth labels;
2. identical decision definitions and output schema;
3. same warm/cold-call policy;
4. same geographic region and network path;
5. same concurrency and batch size;
6. accuracy as well as latency;
7. separate reporting of model execution, application overhead and network latency;
8. authorized use of every compared service and dataset.

Do not use third-party service outputs to train or imitate this project unless the applicable written terms expressly permit it.
