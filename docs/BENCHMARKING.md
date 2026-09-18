# Benchmarking

ROOOMTECH Decision Core 0.3 adds a reproducible benchmark path for local classifiers.

## What the benchmark measures

`POST /v1/benchmarks/local` measures a trained local model against labeled held-out examples and reports:

- top-1 accuracy;
- macro precision, recall and F1;
- per-label precision, recall, F1 and support;
- confusion matrix;
- mean confidence;
- expected calibration error (ECE);
- coverage and selective accuracy at a confidence threshold;
- cold-start latency;
- mean / p50 / p95 / p99 per-item latency;
- throughput in items per second.

The endpoint evaluates examples in memory and does not persist benchmark text.

## API example

```json
{
  "model_id": "mdl_...",
  "examples": [
    {"text": "玄関の鍵が開きません", "label": "access_problem"},
    {"text": "領収書を発行できますか？", "label": "payment_question"}
  ],
  "device": "auto",
  "warmup_runs": 2,
  "repeat_runs": 5,
  "batch_size": 32,
  "confidence_threshold": 0.75,
  "max_errors": 25
}
```

Send it to `POST /v1/benchmarks/local`. If `RTDC_ADMIN_API_KEY` is configured, include `X-RTDC-Admin-Key`.

## Bundled Japanese benchmark

`benchmarks/japanese_hospitality_intent_360.py` deterministically builds 360 independently authored Japanese examples across six hospitality-support intents. It is synthetic benchmark data, not copied customer correspondence and not generated from a third-party proprietary service.

The dataset contains 252 training examples and 108 held-out test examples across six balanced labels.

Run the full train + benchmark flow:

```bash
pip install -e '.[ml]'
python benchmarks/train_and_benchmark_japanese.py
```

The script prints JSON containing training metrics and held-out benchmark metrics.

## Using real business data

For a production evaluation, replace the bundled synthetic dataset with an independently collected and lawfully usable held-out dataset from the actual deployment domain. Do not evaluate on training examples.

Recommended minimum: at least 100 held-out examples, at least 20 examples per important class, manually verified labels, no duplicate or near-duplicate messages across train and test, and separate reporting by language and major intent. Remove or pseudonymize personal information before long-term storage.

## Third-party comparisons

This benchmark intentionally does not call or imitate third-party proprietary decision products. If you compare against another vendor, use a separately authorized account, comply with that vendor's terms, keep the input dataset and network region identical, report accuracy as well as latency, and disclose warm/cold calls, concurrency, batch size and hardware.
