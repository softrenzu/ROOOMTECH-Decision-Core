# v0.7 measured performance baseline — 2026-09-19

This file records a reproducible engineering baseline, not a production performance guarantee and not a comparison against any third-party product.

## Environment

- Commit: `588b6f0779c938416c934aa493998ddeba993e68`
- GitHub Actions hosted runner
- Ubuntu 24.04.5 LTS
- Python 3.12.14
- Runner region: Azure `eastus2`
- API and benchmark client on the same runner via loopback (`127.0.0.1`)
- Fast profile kind: routing
- Fast profile provider: deterministic local `rules`
- Target: 150 ms
- No external model/network provider used by the decision operation

Workflow run: `35377784805`

## Realtime HTTP fast path

5,000 requests at concurrency 64:

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 9.444387 s |
| Throughput | 529.415 requests/s |
| Mean | 120.071 ms |
| p50 | 117.410 ms |
| p95 | 142.467 ms |
| p99 | 168.253 ms |
| Max | 173.333 ms |
| Server-side 150 ms target hit rate | 100% |

The client-observed HTTP p95 was below 150 ms in this run. p99 was above 150 ms. A prior run on a different hosted runner produced HTTP p95 of 154.670 ms, so a strict 150 ms end-to-end HTTP guarantee is not justified from these measurements.

## Realtime WebSocket fast path

5,000 requests over 32 persistent WebSocket connections:

| Metric | Result |
| --- | ---: |
| Errors | 0 |
| Wall time | 0.942776 s |
| Throughput | 5,303.486 requests/s |
| Mean | 5.737 ms |
| p50 | 5.497 ms |
| p95 | 7.825 ms |
| p99 | 9.820 ms |
| Max | 13.624 ms |
| Server-side 150 ms target hit rate | 100% |

This is the preferred transport for latency-sensitive local decision workloads because it avoids a new HTTP request lifecycle for every event.

## 10,000-item Map/Reduce

Synthetic local rules workload, concurrency 64, 10,000 items per run, 3 repeated runs:

| Metric | Result |
| --- | ---: |
| Total items | 30,000 |
| Failed items | 0 |
| Aggregate wall time | 1.930707 s |
| Aggregate throughput | 15,538.350 items/s |
| Mean run latency | 643.555 ms |
| p50 run latency | 643.886 ms |
| p95 run latency | 652.295 ms |
| p99 run latency | 653.042 ms |

Per-run throughput:

- Run 1: 15,308.566 items/s
- Run 2: 15,530.706 items/s
- Run 3: 15,784.081 items/s

## Interpretation

These results establish that the v0.7 fast-path architecture itself has low overhead for local deterministic decisions, and that persistent WebSocket transport is substantially more suitable than repeated HTTP requests for high-frequency realtime traffic on this test environment.

They do **not** establish the latency of a trained local classifier, GPU model, multimodal model, internet-facing deployment, remote model API, or arbitrary customer workload. Those must be measured separately under the intended deployment conditions.
