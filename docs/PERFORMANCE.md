# Performance and latency methodology

ROOOMTECH Decision Core includes a latency-sensitive fast path and explicit load benchmarks. The design goal is to make latency measurable and controllable rather than to publish an unconditional speed claim.

## Realtime fast profiles

A fast profile prevalidates a request template and stores the compiled form in process memory. Each realtime call then supplies only the changing input text. This removes repeated configuration parsing and prevents accidental fallback to a remote model.

Supported network-free profile modes:

- `rules`
- `local_classifier`
- `local_ngram` ranking
- `heuristic` schema extraction

`auto` and `openai_compatible` are rejected for fast profiles because network latency is outside the process and can vary materially.

By default, profile definitions are process-local and are lost on restart. `RTDC_FAST_PROFILE_LIMIT` controls the maximum in-memory profile count. When multiple application processes need to share dynamically-created profiles, enable the optional Redis registry described below. The compiled hot-path object still remains local to each process.

## Bounded realtime scheduler and backpressure

The fast path uses a bounded scheduler instead of allowing an unlimited request backlog.

- `RTDC_FAST_WORKERS` controls concurrent fast-path execution slots. Default: 32.
- `RTDC_FAST_QUEUE_CAPACITY` caps total pending plus in-flight fast-path work. Default: 4096.
- `RTDC_FAST_MAX_QUEUE_WAIT_MS` limits how long a request may wait for an execution slot. Default: 100 ms.

When pending capacity is exhausted or queue wait exceeds the configured limit, `/v1/realtime/fast` returns HTTP 429 rather than allowing latency to grow without bound. WebSocket callers receive the overload as an error result from the realtime dispatcher.

`GET /v1/info` exposes current scheduler counters including pending, in-flight, queue depth, rejections and queue timeouts.

For a strict low-latency application, set `RTDC_FAST_MAX_QUEUE_WAIT_MS` below the application's total latency budget. Capacity should be increased only after measuring CPU/GPU saturation; a larger queue by itself does not create more compute capacity.

## Accelerator-aware local classifier scheduling

CPU and CUDA use different execution strategies because the cost structure is different.

### CPU

CPU requests use bounded direct execution through a thread gate. They intentionally skip the micro-batch collection delay because the current lightweight classifier is usually faster when a request can execute immediately. `RTDC_CPU_INFERENCE_SLOTS` controls the maximum concurrent local CPU inference calls and defaults to the smaller of 8 and the available CPU count.

More CPU threads are not automatically faster. On shared runners, excessive parallelism has produced contention and worse tail latency, so tune this value on the actual deployment hardware.

### CUDA

CUDA requests sharing the same model, allowed choices and device are collected into a short micro-batch and executed through one `predict_many` call.

- `RTDC_LOCAL_BATCH_MAX` controls maximum batch size. Default: 64.
- `RTDC_LOCAL_BATCH_WAIT_MS` controls the collection window. Default: 0.5 ms.
- `RTDC_LOCAL_INFERENCE_QUEUE_CAPACITY` caps the per-model inference queue. Default: 4096.
- `RTDC_GPU_INFERENCE_SLOTS` controls concurrent GPU inference batches. Default: 1.

The GPU gate prevents an unlimited number of concurrent kernel launches and avoids uncontrolled peak memory pressure. A small batch collection window can improve accelerator throughput while adding little delay at low load, but the correct settings depend on the model and traffic distribution.

The scheduling counters and device information are exposed by `GET /v1/accelerator` and under `local_ml` in `GET /v1/info`.

## Multiple server processes

`RTDC_FAST_WORKERS` is an in-process concurrency limit; it is not the number of Uvicorn or Gunicorn processes.

For CPU-heavy deployments, several server processes can be used. Dynamic fast-profile definitions can be shared through Redis:

```bash
pip install -e '.[distributed]'
export RTDC_REDIS_URL=redis://127.0.0.1:6379/0
export RTDC_FAST_PROFILE_REDIS_ENABLED=true
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

`RTDC_FAST_PROFILE_REDIS_KEY` controls the Redis hash key and defaults to `rtdc:fast:profiles`.

Redis contains the validated profile definition and summary, not the compiled hot-path object. If a request reaches a worker that has not seen a profile before, that worker loads the definition once, compiles it locally, optionally prewarms the referenced local model, and then serves subsequent requests from process-local memory.

For a local-classifier profile, each server process must also be able to read the referenced model files. Multiple processes on the same host can share the same model directory. Multi-host deployments need synchronized/shared model storage or an explicit model-distribution mechanism; the Redis profile registry does not replicate model weight files.

For GPU deployments, blindly multiplying server processes can duplicate model memory. Prefer a small number of application processes with the GPU inference gate and measured batching behavior before increasing process count.

## Latency target

The default profile target is 150 ms. Every `/v1/realtime/fast` response reports:

- `queue_ms`: time waiting for a fast-path execution slot;
- `execution_ms`: execution time after a slot was acquired;
- `latency_ms`: `queue_ms + execution_ms`;
- `target_ms`: the configured profile target;
- `within_target`: whether the request completed successfully within the target using total in-process latency.

Do not treat the configured target as a guarantee. Performance varies with CPU/GPU, container limits, input length, classifier size, concurrent load, Python runtime, operating system, process count, and surrounding network/proxy latency.

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

The response includes total measured calls, error count, wall-clock duration, minimum / mean / p50 / p95 / p99 / maximum latency, calls per second and target-hit rate.

The benchmark measures queueing plus execution inside the process under the requested burst load. This is useful for capacity planning, but it does not include client-to-server network transit.

## Transport-inclusive WebSocket benchmark

Use:

```bash
python benchmarks/websocket_realtime_load.py \
  --ws-url ws://127.0.0.1:8000/v1/realtime/ws \
  --profile-id support-route \
  --input 'ログインできません' \
  --requests 10000 \
  --connections 64
```

The result separates:

- client-observed WebSocket round-trip latency;
- server total latency;
- fast-path queue latency;
- server execution latency.

This distinction is important when tuning concurrency because a high total latency can come either from waiting for a fast-path slot or from CPU/GPU contention during execution.

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
