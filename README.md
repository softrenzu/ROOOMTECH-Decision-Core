# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal decision engine that turns unstructured inputs into typed, probabilistic, machine-usable decisions.

Version 0.5 adds reusable **decision operations** so application code can call detection, routing, scoring, verification, ranking/search and probabilistic feature extraction directly. These sit on top of the same confidence, abstention, local-model and optional LLM-fallback system used by the core classifier.

## Core features

- General multi-choice classification through `/v1/decide`
- Property detection with explicit probability thresholds
- Confidence-gated application routing
- Rubric scoring with an expected numeric score
- Multi-check policy / quality verification with `pass`, `fail` and `review`
- Multilingual offline ranking/search using Unicode n-gram similarity
- Optional authorized model-based reranking
- Probabilistic feature extraction for downstream ML models
- Text, image, PDF and audio input
- Image + text score fusion
- Trainable local multilingual text classifier
- CPU or CUDA GPU inference
- Confidence, top-2 margin and normalized entropy gates
- Abstain / human-review behavior
- Local classifier first with optional OpenAI-compatible fallback
- Accuracy, calibration and latency benchmarking
- No raw upload persistence by default
- Personal-use-free / business-use-paid licensing

## Quick start

```bash
cp .env.example .env
pip install -e '.[multimodal]'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`.

For text-only deployments, `pip install -e '.[ml]'` is sufficient.

## Decision operations

The higher-level endpoints are:

```text
POST /v1/ops/detect
POST /v1/ops/route
POST /v1/ops/score
POST /v1/ops/verify
POST /v1/ops/rank
POST /v1/ops/search
POST /v1/ops/features
```

They are designed so control flow remains ordinary application code while the decision layer returns probabilities and review signals. See `docs/DECISION_OPERATIONS.md`.

Example routing request:

```json
{
  "input": "ログインできません。パスワードを再設定したいです",
  "provider": "rules",
  "routes": [
    {"id": "billing", "keywords": ["請求"]},
    {"id": "account", "keywords": ["ログイン", "パスワード"]}
  ]
}
```

## Multimodal decisions

`POST /v1/multimodal/decide` accepts text together with images, PDFs and audio. No third-party vision or speech model weights are bundled; deployers configure models they are authorized to use. See `docs/MULTIMODAL.md`.

## Local model and benchmarks

`POST /v1/models/train` trains the lightweight local text classifier. `POST /v1/benchmarks/local` measures accuracy, macro-F1, calibration, p50/p95/p99 latency and throughput. The repository benchmark data is independently authored synthetic data; production claims should use lawful held-out real data.

## Dify and application integration

Use ordinary HTTP Request nodes against the JSON endpoints. File inputs use the multipart `/v1/multimodal/decide` endpoint. The operation endpoints are intentionally small so they can also be called directly from code without adopting a workflow framework.

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`.

Any use by or for a company, corporation, partnership, nonprofit, government body, educational institution, employer, client, sole proprietorship, or other business/professional activity requires a paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

Commercial licensing: support@rooomtech.com

## Independence

ROOOMTECH Decision Core is an independent product. It does not include third-party proprietary source code, prompts, private APIs, decision-service outputs, copied benchmark data or third-party UI assets. It is not marketed as a clone, successor or official compatible implementation of another vendor's product. See `NOTICE.md`, `BRAND_GUIDELINES.md` and `docs/LEGAL_DESIGN.md`.

## Version

`0.5.0`
