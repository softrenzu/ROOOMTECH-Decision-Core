# v0.7 measured performance baseline — 2026-09-19

This file records reproducible engineering baselines, not production performance guarantees and not comparisons against any third-party product.

## Rules fast-path environment

- Commit: `a080fb3fbdb212b87b38229828867d7131eb21eb`
- GitHub Actions hosted runner
- Ubuntu 24.04.5 LTS
- Python 3.12.14
- Runner region: Azure `eastus2`
- API and benchmark client on the same runner via loopback (`127.0.0.1`)
- Fast profile kind: routing
- Fast profile provider: deterministic local `rules`
- Target: 150 ms
- No external model/network provider used by the decision operation
- Workflow run: `35378291135`

## Rules — HTTP fast path

5,000 requests at concurrency 64:

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 8.076237 s |
| Throughput | 619.100 requests/s |
| Round-trip mean | 102.639 ms |
| Round-trip p50 | 101.142 ms |
| Round-trip p95 | 131.174 ms |
| Round-trip p99 | 139.120 ms |
| Round-trip max | 148.617 ms |
| Server execution mean | 0.081 ms |
| Server execution p50 | 0.079 ms |
| Server execution p95 | 0.117 ms |
| Server execution p99 | 0.142 ms |
| Server execution max | 0.393 ms |
| Server-side 150 ms target hit rate | 100% |

The HTTP transport dominates this local-rules workload. Hosted-runner HTTP results have varied between runs, so a strict universal end-to-end HTTP guarantee is not justified from these measurements.

## Rules — WebSocket fast path

5,000 requests over 32 persistent WebSocket connections:

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 0.754984 s |
| Throughput | 6,622.655 requests/s |
| Round-trip mean | 4.533 ms |
| Round-trip p50 | 4.367 ms |
| Round-trip p95 | 5.936 ms |
| Round-trip p99 | 7.663 ms |
| Round-trip max | 12.884 ms |
| Server execution mean | 0.051 ms |
| Server execution p50 | 0.046 ms |
| Server execution p95 | 0.078 ms |
| Server execution p99 | 0.105 ms |
| Server execution max | 0.169 ms |
| Server-side 150 ms target hit rate | 100% |

Persistent WebSocket is the intended transport for high-frequency realtime local decisions.

## Rules — 10,000-item Map/Reduce

Synthetic local rules workload, concurrency 64, 10,000 items per run, 3 repeated runs:

| Metric | Result |
| --- | ---: |
| Total items | 30,000 |
| Failed items | 0 |
| Aggregate wall time | 1.579564 s |
| Aggregate throughput | 18,992.584 items/s |
| Mean run latency | 526.507 ms |
| p50 run latency | 525.549 ms |
| p95 run latency | 532.755 ms |
| p99 run latency | 533.396 ms |

Per-run throughput:

- Run 1: 19,215.394 items/s
- Run 2: 19,027.737 items/s
- Run 3: 18,742.167 items/s

## Local-classifier environment

- Commit: `19a32503b2e0d282d9d5a2a216be0098c373ac8a`
- GitHub Actions hosted runner
- Ubuntu 24.04.5 LTS
- Python 3.12.14
- CPU-only PyTorch 2.14.0
- Runner region: Azure `westcentralus`
- API and benchmark client on the same runner via loopback
- Fast profile kind: routing
- Fast profile provider: `local_classifier`
- Classifier: 2 labels, Unicode character n-gram features, feature dimension 1024
- Synthetic training examples: 160
- Train accuracy: 100%
- Validation accuracy: 100%
- Prewarmed: yes
- Target: 150 ms
- Workflow run: `35378327304`

The 100% accuracy values above apply only to this small synthetic performance dataset and are not a production accuracy claim.

## Local classifier — WebSocket fast path

5,000 requests over 32 persistent WebSocket connections:

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 2.904890 s |
| Throughput | 1,721.235 requests/s |
| Round-trip mean | 18.237 ms |
| Round-trip p50 | 18.119 ms |
| Round-trip p95 | 19.122 ms |
| Round-trip p99 | 23.508 ms |
| Round-trip max | 27.194 ms |
| Server execution mean | 0.430 ms |
| Server execution p50 | 0.424 ms |
| Server execution p95 | 0.469 ms |
| Server execution p99 | 0.610 ms |
| Server execution max | 0.841 ms |
| Server-side 150 ms target hit rate | 100% |

This is the most relevant current benchmark for the trainable local AI decision path. It stayed well below the 150 ms target on this controlled loopback workload.

## Saturation / queueing benchmark

1,000 local-classifier calls were enqueued as a burst with concurrency 32. This benchmark measures queue wait plus execution rather than raw inference latency.

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 0.395526 s |
| Throughput | 2,528.276 calls/s |
| Queue-to-completion mean | 197.404 ms |
| Queue-to-completion p50 | 197.271 ms |
| Queue-to-completion p95 | 375.031 ms |
| Queue-to-completion p99 | 390.916 ms |
| 150 ms queue-to-completion hit rate | 38% |

This does not contradict the WebSocket result: under a burst larger than available concurrency, later calls wait in the queue. Capacity planning and backpressure are required if an application needs every event to meet a fixed deadline under saturation.

## CI regression gates

The repository now runs performance workflows with explicit gates.

Rules performance smoke requires:

- zero HTTP and WebSocket errors;
- at least 99.9% server-side 150 ms target hits;
- WebSocket round-trip p95 below 150 ms;
- WebSocket server p95 below 150 ms;
- Map/Reduce throughput above 10,000 items/s with zero failed items.

Local-classifier performance requires:

- zero WebSocket errors;
- at least 99.9% server-side 150 ms target hits;
- local-classifier WebSocket round-trip p95 below 150 ms;
- local-classifier server p95 below 150 ms.

Both measured workflows passed these gates on 2026-09-19.

## Interpretation

These results establish that the v0.7 fast-path architecture has low execution overhead for local rules and the current lightweight local classifier, and that persistent WebSocket transport is substantially better suited than repeated HTTP requests for high-frequency realtime traffic in this test environment.

They do **not** establish the latency or accuracy of a GPU model, multimodal model, internet-facing deployment, remote model API, arbitrary customer workload, or third-party system. Those must be benchmarked separately under the intended production conditions.
