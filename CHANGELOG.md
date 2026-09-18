# Changelog

## 0.3.0 - 2026-09-18

- Added local classifier benchmark API
- Added top-1 accuracy and macro precision/recall/F1
- Added per-label metrics and confusion matrix
- Added confidence calibration error (ECE)
- Added confidence-threshold coverage and selective accuracy
- Added cold-start, mean, p50, p95 and p99 per-item latency
- Added throughput measurement
- Added bounded misclassification metadata without returning benchmark text
- Added 360-example independently authored synthetic Japanese hospitality benchmark
- Added turnkey train + held-out benchmark script
- Added benchmark documentation and third-party comparison guardrails
- Benchmark examples are evaluated in memory and are not persisted

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
