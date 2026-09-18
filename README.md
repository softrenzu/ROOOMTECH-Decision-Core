# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed structured-decision engine for turning unstructured text into typed, machine-usable decisions.

Version 0.2 adds a trainable lightweight local classifier that works directly with Unicode character n-grams. It supports Japanese and other languages without relying on a third-party proprietary decision model, and can run inference on CPU or NVIDIA CUDA GPU.

## Core features

- Train your own classification model from labeled CSV/JSON-style examples through the API
- Japanese / English / Chinese / Korean / mixed-language input without tokenizer dictionaries
- CPU or CUDA GPU inference with automatic device selection
- Multiple typed decisions in one API request
- Probability distribution for every choice
- Confidence, top-2 margin and normalized entropy gates
- Abstain / human-review behavior instead of forcing low-confidence decisions
- Local classifier first, optional LLM fallback only when needed
- Deterministic rules provider for zero-model deployments
- OpenAI-compatible provider abstraction for authorized external models
- Batch API and Dify-friendly HTTP interface
- No raw training-text persistence by the local training API
- Personal-use-free / business-use-paid licensing
- Optional signed commercial-license enforcement

## Quick start without local ML

```bash
cp .env.example .env
docker compose up --build
```

## Local ML / Japanese classifier

```bash
pip install -e '.[ml]'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For NVIDIA GPU deployments:

```bash
cp .env.example .env
docker compose -f docker-compose.gpu.yml up --build
```

Open `http://localhost:8000/docs`.

Train the Japanese demo model:

```bash
curl -X POST http://localhost:8000/v1/models/train \
  -H 'content-type: application/json' \
  --data-binary @examples/train_japanese_intent.json
```

The response returns `model_id`. Put that ID into a decision:

```json
{
  "input": "お湯が出なくて困っています",
  "provider": "auto",
  "decisions": [
    {
      "id": "guest_intent",
      "question": "問い合わせ種別",
      "choices": ["checkin_question", "equipment_problem", "complaint"],
      "model_id": "MODEL_ID",
      "min_confidence": 0.78
    }
  ]
}
```

If the local classifier is confident enough, the request finishes locally. If confidence gates fail and an OpenAI-compatible endpoint is configured, `auto` mode falls back to the LLM.

See `docs/TRAINING_API.md` for training, GPU and security details.

## Dify

Use an HTTP Request node against `/v1/decide`. Branch on `results[].requires_review`, `results[].selected`, or `results[].confidence`.

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`.

Any use by or for a company, corporation, partnership, nonprofit, government body, educational institution, employer, client, sole proprietorship, or other business/professional activity requires a paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

Commercial licensing: support@rooomtech.com

## Independence

ROOOMTECH Decision Core is an independent product. It does not include or depend on third-party proprietary source code, model weights, outputs, private APIs or benchmark data, and it is not marketed as a clone, successor or official compatible implementation of another vendor's product. See `NOTICE.md`, `BRAND_GUIDELINES.md` and `docs/LEGAL_DESIGN.md`.

## Version

`0.2.0`
