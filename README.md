# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal structured-decision engine for turning unstructured inputs into typed, machine-usable decisions.

Version 0.4 adds one decision surface for **text + images + PDFs + audio**. Text can be combined with one or more images in the same request, PDFs can be routed through extracted text or rendered-page vision, and audio is transcribed locally before the normal confidence-gated decision flow.

## Core features

- Text, image, PDF and audio input through one API
- Image + text score fusion for a single typed decision
- PDF embedded-text extraction with optional rendered-page vision fallback
- Local audio transcription before classification
- Train your own text classification model from labeled examples
- Japanese / English / Chinese / Korean / mixed-language text support
- CPU or CUDA GPU inference with automatic device selection
- Multiple typed decisions in one request
- Probability distribution for every choice
- Confidence, top-2 margin and normalized entropy gates
- Abstain / human-review behavior instead of forcing low-confidence decisions
- Local classifier first, optional LLM fallback only when needed
- Deterministic rules provider for zero-model deployments
- OpenAI-compatible provider abstraction for authorized external models
- Accuracy, macro-F1, calibration and latency benchmarking
- No raw upload persistence by default
- Personal-use-free / business-use-paid licensing
- Optional signed commercial-license enforcement

## Quick start

```bash
cp .env.example .env
pip install -e '.[multimodal]'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`.

For text-only deployments, `pip install -e '.[ml]'` is sufficient.

## Multimodal example

Configure a CLIP-compatible vision model that you are authorized to use:

```env
RTDC_VISION_MODEL=<model-id>
RTDC_VISION_DEVICE=auto
```

Then send text and an image together:

```bash
curl -X POST http://localhost:8000/v1/multimodal/decide \
  -F 'text=The customer says this item arrived broken.' \
  -F 'decisions_json=[{"id":"condition","question":"What is the item condition?","choices":["normal","needs_attention","damaged"]}]' \
  -F 'files=@photo.jpg'
```

The same endpoint accepts PDFs and common audio formats. See `docs/MULTIMODAL.md`.

## Local text classifier

Train a Japanese demo model:

```bash
curl -X POST http://localhost:8000/v1/models/train \
  -H 'content-type: application/json' \
  --data-binary @examples/train_japanese_intent.json
```

The response returns `model_id`. Put that ID into a decision. If the local classifier is confident enough, the request finishes locally. If confidence gates fail and an OpenAI-compatible endpoint is configured, `auto` mode can fall back to the LLM.

## Benchmark accuracy and speed

A trained local model can be evaluated through `POST /v1/benchmarks/local`.

The benchmark reports accuracy, macro precision/recall/F1, per-label metrics, confusion matrix, calibration error, confidence-threshold coverage, cold-start latency, mean/p50/p95/p99 per-item latency and throughput.

The repository includes an independently authored synthetic Japanese benchmark generator with 360 examples, 252 training examples and 108 held-out test examples. For production claims, replace synthetic examples with lawfully collected real held-out data. See `docs/BENCHMARKING.md`.

## Dify

Use an HTTP Request node against `/v1/decide` for text-only decisions, or a multipart HTTP request against `/v1/multimodal/decide` when files are involved.

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`.

Any use by or for a company, corporation, partnership, nonprofit, government body, educational institution, employer, client, sole proprietorship, or other business/professional activity requires a paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

Commercial licensing: support@rooomtech.com

## Independence

ROOOMTECH Decision Core is an independent product. It does not include third-party proprietary source code, private APIs, decision-service outputs or benchmark data, and it is not marketed as a clone, successor or official compatible implementation of another vendor's product. No third-party vision or speech model weights are bundled. See `NOTICE.md`, `BRAND_GUIDELINES.md` and `docs/LEGAL_DESIGN.md`.

## Version

`0.4.0`
