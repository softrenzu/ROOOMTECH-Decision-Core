# ROOOMTECH Decision Core

ROOOMTECH Decision Core is an independently developed multimodal decision engine for typed, probabilistic, machine-usable decisions.

Version 0.6 adds arbitrary JSON Schema extraction, large parallel Map/Reduce with optional Redis-based distributed workers, NDJSON streaming, and a realtime WebSocket decision API.

## Main capabilities

- Classification, detection, routing, scoring and verification
- Ranking/search and probabilistic feature extraction
- Arbitrary JSON Schema structured extraction with validation
- Local Map/Reduce over up to 100,000 items
- Optional Redis-sharded distributed Map/Reduce workers
- Streaming Map/Reduce results over NDJSON
- WebSocket realtime decisions and HTTP NDJSON event streaming
- Text, image, PDF and audio input
- Trainable local multilingual classifier with CPU/CUDA support
- Confidence, margin, entropy, abstention and human-review gates
- Personal-use-free / business-use-paid licensing

## New endpoints

```text
POST /v1/extract
POST /v1/mapreduce/run
POST /v1/mapreduce/stream
POST /v1/mapreduce/jobs
GET  /v1/mapreduce/jobs/{job_id}
WS   /v1/realtime/ws
POST /v1/realtime/stream
```

See `docs/ADVANCED_PIPELINES.md` for examples and operational/security notes.

## Quick start

```bash
cp .env.example .env
pip install -e '.[multimodal]'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For Redis workers:

```bash
pip install -e '.[distributed]'
python -m app.mapreduce_worker
```

## Existing decision operations

```text
POST /v1/decide
POST /v1/ops/detect
POST /v1/ops/route
POST /v1/ops/score
POST /v1/ops/verify
POST /v1/ops/rank
POST /v1/ops/search
POST /v1/ops/features
POST /v1/multimodal/decide
```

## Licensing

Natural-person personal, non-business use is available under `LICENSE_PERSONAL.md`. Business, professional, organizational or institutional use requires a separate paid commercial license from ROOOMTECH. See `COMMERCIAL_LICENSE.md`.

## Independence

This is an independent product. It does not include third-party proprietary source code, prompts, private APIs, decision-service outputs, copied benchmark data or third-party UI assets, and it is not marketed as a clone or official compatible implementation of another vendor's product.

## Version

`0.6.0`
