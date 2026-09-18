# Local training API

ROOOMTECH Decision Core 0.2 adds an independent, trainable lightweight classifier designed for fast domain-specific decisions.

## Why this model is different

The local classifier does not copy, distill, query or imitate a third-party proprietary decision model. It uses deterministic Unicode character n-gram hashing plus a small linear neural classifier trained only from examples supplied by the operator.

This design has several practical properties:

- Japanese works without a morphological tokenizer.
- English, Chinese, Korean and mixed-language text use the same feature extractor.
- Training is fast for small and medium domain datasets.
- Inference can run on CPU or CUDA GPU.
- The training API does not persist raw training text; it stores only learned weights and metadata.
- Validation examples are used, when available, to fit a temperature value for better-calibrated softmax probabilities.

This is a lightweight domain classifier, not a general language-understanding model. For ambiguous or semantic cases, `provider: auto` can fall back to a configured LLM.

## Install ML support

```bash
pip install -e '.[ml]'
```

GPU Docker:

```bash
cp .env.example .env
docker compose -f docker-compose.gpu.yml up --build
```

The host requires a working NVIDIA driver and NVIDIA Container Toolkit.

## Train a model

```bash
curl -X POST http://localhost:8000/v1/models/train \
  -H 'content-type: application/json' \
  -H 'X-RTDC-Admin-Key: YOUR_ADMIN_KEY' \
  --data-binary @examples/train_japanese_intent.json
```

The response includes a generated `model_id`. Raw training text is processed in memory and is not written to the model directory by this API.

## Predict directly

```bash
curl -X POST http://localhost:8000/v1/models/MODEL_ID/predict \
  -H 'content-type: application/json' \
  -d '{"inputs":["エアコンが壊れています","チェックインは何時ですか"],"device":"auto"}'
```

## Use the model in the decision engine

```json
{
  "input": "エアコンが動かなくて困っています",
  "provider": "auto",
  "decisions": [
    {
      "id": "guest_intent",
      "question": "問い合わせ種別",
      "choices": ["checkin_question", "equipment_problem", "complaint"],
      "model_id": "MODEL_ID",
      "min_confidence": 0.78,
      "min_margin": 0.12,
      "max_entropy": 0.8
    }
  ]
}
```

In `auto` mode the local classifier is tried first when a `model_id` is supplied. If the result falls below the review thresholds and an OpenAI-compatible provider is configured, the engine calls the LLM as a fallback. If the local model is confident enough, no LLM call is made.

## Security

Set `RTDC_ADMIN_API_KEY` on any shared deployment. Training and model-list/detail endpoints require the `X-RTDC-Admin-Key` header when the environment variable is set.

The prediction and decision endpoints are intentionally separate from admin authentication so deployments can place their own API gateway, OAuth or service-to-service authentication in front of the service.

## Privacy note

The training API does not save the submitted raw text. It saves a weight file and model metadata. Learned models can still encode statistical information about training data, so highly sensitive datasets should be handled under the operator's own security and privacy controls.
