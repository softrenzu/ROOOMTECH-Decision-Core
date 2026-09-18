# Changelog

## 0.9.0 - 2026-09-19

- Added independently designed ROOOMTECH Decision Studio at `/studio`
- Added empirical calibration analysis with ECE, Brier score, reliability bins and threshold/coverage curves
- Added target-error threshold recommendation from operator-owned held-out outcomes
- Added privacy-conscious human-review queue backed by SQLite for single-node deployments
- Raw review input is not retained by default; SHA-256 correlation digest is kept instead
- Added governed decision endpoint that automatically routes low-confidence, abstained or review-required outcomes to human review
- Added resolved-review export for operator-approved active-learning datasets when raw input retention was explicitly enabled
- Added Studio API-key protection and review retention/purge controls
- Wired shared realtime profile registry startup/shutdown so Redis invalidation listeners run with the API lifecycle
- Added independent-product-development policy and stronger release/legal separation controls

## 0.7.0 - 2026-09-19

- Added prevalidated realtime fast profiles for latency-sensitive workloads
- Fast profiles reject external-network provider modes and support rules, local classifiers, local n-gram ranking and heuristic extraction
- Added local classifier prewarming during profile creation
- Added per-request `target_ms` and `within_target` reporting with a default 150 ms target
- Added HTTP fast endpoint and WebSocket `fast` event support
- Added realtime burst benchmark with p50/p95/p99 latency, throughput, errors and target-hit rate
- Added repeated Map/Reduce load benchmark with per-run items/second and failure counts
- Added transport-inclusive HTTP load-test utility and performance methodology documentation
- Added optional realtime API-key enforcement to the NDJSON realtime endpoint as well as WebSocket/fast endpoints

## 0.6.0 - 2026-09-19

- Added arbitrary JSON Schema structured extraction with output validation
- Added local heuristic extraction for JSON and key/value text with localized field aliases
- Added optional OpenAI-compatible schema extraction fallback
- Added generic parallel Map/Reduce for decision, detection, routing, scoring, verification, feature extraction and schema extraction
- Added collect/count/sum/average/min/max/top-k reducers
- Added NDJSON Map/Reduce streaming
- Added optional Redis-sharded distributed Map/Reduce jobs and worker process
- Added realtime WebSocket decision API and NDJSON realtime batch streaming
- Added message-size and optional realtime API-key controls
- Added tests and deployment/privacy documentation for the new pipeline features

## 0.5.0 - 2026-09-19

- Added reusable detection, routing, scoring, verification, ranking/search and probabilistic feature extraction

## 0.4.0 - 2026-09-18

- Added unified multimodal decision endpoint for text, images, PDFs and audio

## 0.3.0 - 2026-09-18

- Added reproducible local-model accuracy and latency benchmarking

## 0.2.0 - 2026-09-18

- Added trainable local Unicode character n-gram classifier and CPU/CUDA support

## 0.1.0 - 2026-09-18

- Initial independent implementation
