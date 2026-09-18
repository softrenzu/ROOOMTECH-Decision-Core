# Changelog

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
