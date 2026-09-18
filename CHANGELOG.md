# Changelog

## 0.5.0 - 2026-09-19

- Added reusable detection primitive with probability thresholds
- Added confidence-gated routing to explicit application code paths
- Added rubric scoring with probability-weighted expected numeric scores
- Added multi-check policy and quality verification with pass/fail/review states
- Added multilingual offline Unicode n-gram ranking/search
- Added optional OpenAI-compatible model reranking when explicitly requested
- Added probabilistic feature extraction for downstream classical ML/statistical models
- Added deterministic keyword support to detection, routing, scoring and verification
- Added operation tests and product-separation documentation

## 0.4.0 - 2026-09-18

- Added unified multimodal decision endpoint for text, images, PDFs and audio
- Added simultaneous image + text probability fusion
- Added configurable local CLIP-compatible vision scoring without bundling third-party weights
- Added local PDF text extraction and rendered-page vision fallback for sparse/scanned PDFs
- Added local faster-whisper audio transcription with immediate temporary-file cleanup
- Added per-file and per-request upload limits
- Added multimodal privacy/security documentation and tests
- Kept confidence, margin, entropy and abstention gates after multimodal fusion

## 0.3.0 - 2026-09-18

- Added reproducible local-model benchmark runner
- Added accuracy, macro precision/recall/F1 and per-label metrics
- Added confusion matrix and expected calibration error
- Added confidence-threshold coverage and selective accuracy
- Added cold-start, mean, p50, p95 and p99 latency reporting
- Added throughput measurement
- Added independent synthetic Japanese benchmark generator with 360 examples and a held-out test split
- Added benchmark API and documentation

## 0.2.0 - 2026-09-18

- Added trainable local Unicode character n-gram classifier
- Japanese, English, Chinese, Korean and mixed-language text support without tokenizer dependencies
- Added CUDA GPU / CPU automatic device selection
- Added local model training, listing and prediction APIs
- Added temperature calibration using held-out validation examples when available
- Added per-decision `model_id` routing
- `auto` mode now prefers a local classifier and falls back to an external LLM only when confidence gates fail
- Raw training text is not persisted by the training API
- Added optional admin API key for training/model-management endpoints
- Added GPU Docker deployment files
- Added Japanese training example and local-feature tests
- Removed the legacy repository-name dependency from product design

## 0.1.0 - 2026-09-18

- Initial independent implementation
- Multi-axis typed decisions
- Per-choice probability output
- Confidence, margin and normalized entropy
- Human-review and abstain gates
- Evidence snippets and reason codes without chain-of-thought
- Offline rules provider
- OpenAI-compatible model provider
- Automatic rules-first cascade
- Batch endpoint
- Dify HTTP example
- Signed commercial-license verification option
- Personal-use-free / business-use-paid licensing files
- Brand and independence guardrails
