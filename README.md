# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed structured-decision API for turning unstructured text into typed, machine-usable decisions.

It is not a chat product and it does not depend on any third-party proprietary decision model. The engine can run fully locally with rules, or call any OpenAI-compatible endpoint such as vLLM, LM Studio, Ollama OpenAI compatibility, or a hosted model endpoint that you are licensed to use.

## What is new

- Multiple decisions in one request
- Probability distribution for every choice
- Abstain / human-review gate based on confidence, margin and entropy
- Evidence snippets instead of hidden chain-of-thought
- Hybrid mode: deterministic rules first, model fallback only when needed
- Batch API
- OpenAI-compatible provider abstraction
- Dify-friendly HTTP interface
- No input persistence by default
- Personal-use-free / business-use-paid dual licensing
- Optional signed commercial-license enforcement

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Then open `http://localhost:8000/docs`.

Example:

```bash
curl -s http://localhost:8000/v1/decide \
  -H 'content-type: application/json' \
  -d '{
    "input": "The guest says the room has no hot water and wants help now.",
    "decisions": [
      {
        "id": "intent",
        "question": "What is the main intent?",
        "choices": ["checkin_question", "equipment_problem", "complaint", "other"],
        "keywords": {
          "equipment_problem": ["no hot water", "broken", "not working"],
          "complaint": ["refund", "angry", "complaint"]
        }
      },
      {
        "id": "urgency",
        "question": "How urgent is this?",
        "choices": ["low", "medium", "high"],
        "keywords": {
          "high": ["now", "immediately", "locked out", "no hot water"]
        }
      }
    ]
  }'
```

## Providers

`rules` works offline. `openai_compatible` calls a configured `/v1/chat/completions` endpoint. `auto` uses rules first and only calls the model when the rule result is not strong enough.

Set these environment variables when using a model endpoint:

```env
RTDC_MODEL_BASE_URL=http://localhost:11434/v1
RTDC_MODEL_API_KEY=ollama
RTDC_MODEL_NAME=qwen3:8b
```

## Dify

Use an HTTP Request node:

- Method: `POST`
- URL: `http://your-host:8000/v1/decide`
- JSON body: see `examples/dify_request.json`

Branch on `results[].requires_review`, `results[].selected`, or `results[].confidence`.

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`.

Any use by or for a company, corporation, partnership, nonprofit, government body, educational institution, employer, client, sole proprietorship, or other business/professional activity requires a paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

Commercial licensing: support@rooomtech.com

## Brand and independence

The product name is **ROOOMTECH Decision Core**. Do not market it as a clone, successor, compatible implementation, or upgraded edition of any third-party product. No third-party source code, model weights, outputs, private APIs, or benchmark data are included. See `NOTICE.md` and `BRAND_GUIDELINES.md`.

The current repository URL contains a legacy name. For cleaner branding, rename the GitHub repository to `ROOOMTECH-Decision-Core` before public promotion.

## Version

`0.1.0` — initial independent release.
