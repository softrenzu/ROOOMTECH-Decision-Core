# Changelog

## 0.14.0 - 2026-09-19

- Added a local many-to-many Semantic Matrix for high-cardinality candidate generation without external model calls
- Added sparse Unicode character n-gram signatures, deterministic hashing and an inverted feature index
- Added explicit logical-pair bounds to prevent accidental quadratic workloads
- Added a protected same-origin Website Intelligence crawler and internal-link graph audit
- Added internal-link recommendations using the local Semantic Matrix, including literal target-anchor-presence signals
- Added fetched broken-link detection, orphan-page detection and crawl/page/link telemetry
- Website crawling is disabled by default and requires an admin secret outside enterprise mode
- Added a dedicated enterprise `web` project-key scope with quota/audit integration
- Added SSRF-oriented controls: public-address validation, same-origin-only redirects/crawl, ports 80/443 only, no URL credentials, bounded bytes/pages/concurrency/timeouts and robots.txt support
- Fetched page bodies remain in memory for the request and are not persisted by the website-intelligence module
- Added Semantic Matrix, HTML parsing and private-address rejection tests
- Added `docs/WEB_INTELLIGENCE.md` and independent-development boundaries for website/semantic workflows

## 0.13.0 - 2026-09-19

- Added a project resource-ownership registry for persisted datasets, human-review items and local models
- A dataset, review or model resource ID can belong to only one enterprise project
- Added project-scoped dataset CRUD, examples, held-out training/evaluation lineage and same-project review import APIs
- Added project-scoped Human Review Queue, active-learning candidate and training-example export APIs
- Added project-scoped model listing, inspection and direct prediction APIs
- Dataset training automatically registers the resulting model to the dataset owner's project
- Cross-project resource lookups return not-found rather than exposing the owner
- General local-classifier decisions inherit authenticated tenant context so explicit model IDs cannot cross the project boundary
- Administrative model promotion now claims or confirms model ownership and rejects models owned by another project
- Added project-key authentication to the realtime WebSocket path in enterprise mode
- WebSocket keys are revalidated per message so quota, revocation, expiry and project disablement apply to long-lived connections
- Added per-message metadata-only WebSocket audit records and tenant context during dispatch
- Enterprise management APIs now fail closed when required admin/studio secrets are not configured
- Added tenant-boundary tests covering datasets, reviews and model prediction
- Added `docs/TENANT_ISOLATION.md` and updated enterprise-control-plane documentation
- Clarified that v0.13 is an application authorization boundary; the reference SQLite stores are shared and production SaaS should add storage-layer tenant controls

## 0.12.0 - 2026-09-19

- Added reference enterprise projects with enable/disable state and configurable daily request quotas
- Added high-entropy project API keys with scopes, optional expiry and revocation
- Raw project API keys are returned only once; only SHA-256/HMAC-SHA-256 digests are stored
- Added optional HTTP project-key enforcement for inference, guardrail and realtime routes
- Added per-project daily request quota enforcement with HTTP 429 on exhaustion
- Added metadata-only audit logging for authenticated project requests without storing request bodies
- Added local-model promotion records by project, decision ID and environment
- Added model deployment version history and rollback to the most recent historical deployment
- Added project-visible deployment lookup API
- Added enterprise configuration and deployment/security documentation
- Explicitly documented that v0.12 project controls are not yet hard multi-tenant data isolation for datasets/reviews/models
- Guardrail auth now avoids unnecessary duplicate credentials when enterprise project-key enforcement is active

## 0.11.0 - 2026-09-19

- Added an independently designed guardrail gateway for AI input, output and proposed tool-call screening
- Added deterministic prompt-injection-like, sensitive-data, credential-like and risky-action screening signals
- Added custom operator-defined policies with keyword rules and optional RTDC semantic detection
- Added tool-call allowlists/blocklists and explicit-authorization gating for risky actions
- Added local RAG citation/context screening with claim-level pass/review/fail results
- Retrieved-context prompt-injection screening now routes unsafe context to review
- Unsupported or weakly supported cited claims route to review rather than being treated as safe
- Added separate guardrail API-key configuration with realtime/admin fallback
- Guardrail requests are not persisted by the guardrail module
- Added guardrail tests and independent-development documentation
- Guardrail results are explicitly documented as decision-support signals, not security, compliance, or factuality guarantees

## 0.10.0 - 2026-09-19

- Added governed dataset registry with required provenance and operator rights attestation
- Added explicit train/validation/test splits and duplicate-text prevention across splits
- Added deterministic dataset fingerprints for reproducible model lineage
- Added dataset-to-model training workflow using only the explicit training split
- Added optional held-out validation/test benchmarking after training
- Added model-lineage records containing the dataset fingerprint, training metadata and evaluation metrics
- Added resolved Human Review Queue import into governed datasets
- Added uncertainty-prioritized active-learning candidate API
- Added dataset retention and purge controls
- Added dataset governance documentation and a dedicated local dataset store configuration
- Kept the independent-development boundary: no third-party proprietary service outputs are required or collected for training

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