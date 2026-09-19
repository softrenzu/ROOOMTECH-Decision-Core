# Decision Accelerator

ROOOMTECH Decision Core v0.22 adds a machine-oriented typed judgment endpoint for workloads where a large language model should not spend free-form generation tokens on every small decision.

The implementation is independently designed around general classification, scoring, calibration, concurrency, and model-routing patterns. It does not reproduce another vendor's sampler, training algorithm, API schema, prompts, hosted behavior, model weights, or benchmark corpus.

## API

```text
POST /v1/project/accelerate
```

Enterprise mode requires a project key with the `inference` scope. Outside enterprise mode, configure `RTDC_ACCELERATOR_API_KEY`; when blank, the endpoint falls back to the realtime key and then the admin key.

## Judgment types

RTDC uses its own generic judgment schema:

- `boolean`: a probability distribution over `true` and `false`.
- `categorical`: a probability distribution over operator-defined option IDs.
- `scalar`: a probability distribution over operator-defined numeric levels plus an expected numeric value.

These are typed outputs, not free-form prose. Every result also includes confidence, margin, normalized entropy, review/abstention state, provider, and reason codes.

## Automatic routing

`routing_mode=auto` performs the following independent RTDC workflow:

1. Evaluate rule baselines for every judgment.
2. Evaluate configured local classifiers concurrently.
3. Apply each judgment's confidence/margin/entropy acceptance policy.
4. Keep locally resolved judgments local.
5. Rank unresolved judgments by uncertainty.
6. Send only a bounded number of unresolved judgments to the configured external model, in at most one batched model request.
7. Blend the external result with the local baseline and run the acceptance policy again.
8. Leave unresolved outcomes in review/abstain state rather than fabricating certainty.

`max_external_judgments` caps external escalation. `deadline_ms` limits how long the accelerator waits for the external batch; on timeout, RTDC falls back to the local decision/review state.

`routing_mode=local_only` guarantees this endpoint does not call the configured external model. `routing_mode=external_only` is available for controlled comparison or migration, but it is not the recommended cost-saving mode.

## Example

```json
{
  "state": "urgent refund request from premium customer",
  "routing_mode": "auto",
  "max_external_judgments": 4,
  "judgments": [
    {
      "id": "refund_requested",
      "kind": "boolean",
      "instruction": "Is a refund being requested?",
      "true_keywords": ["refund"],
      "min_confidence": 0.8
    },
    {
      "id": "priority",
      "kind": "categorical",
      "instruction": "Choose the support priority",
      "options": [
        {"id": "high", "keywords": ["urgent"]},
        {"id": "normal", "keywords": ["routine"]}
      ]
    },
    {
      "id": "customer_value",
      "kind": "scalar",
      "instruction": "Estimate the customer value band",
      "levels": [
        {"id": "low", "value": 10},
        {"id": "high", "value": 100, "keywords": ["premium"]}
      ]
    }
  ]
}
```

## What this does and does not claim

The accelerator removes unnecessary natural-language output from RTDC's own typed-decision path and can reduce external-model calls when local rules or classifiers are confident enough. A local-only result has no external token billing, although local compute still has a real infrastructure cost.

When RTDC escalates to an external LLM, that provider's normal input/output token pricing still applies. RTDC therefore does **not** claim that external-model output cost is zero.

The implementation runs independent local classifiers concurrently and batches unresolved typed judgments into one external request. It does **not** claim to implement a proprietary neural parallel sampler or a proprietary calibrated-decision reinforcement-learning algorithm. Those are model-training/architecture questions, not properties that can be honestly created by renaming a workflow endpoint.

A single judgment instruction can be used zero-shot through rules or an external model, but that does not mean one prompt universally replaces fine-tuning. Production quality must be measured on held-out operator-owned outcomes, and thresholds should be calibrated for the deployment.

## Calibration

Confidence should be treated as useful only when tested against real outcomes. RTDC's Calibration Studio can measure Brier score, ECE, reliability bins, threshold coverage, and selective accuracy from operator-supplied held-out examples. Recommended automation thresholds should be chosen from those measurements rather than from marketing latency or accuracy claims.

## RAG and long context

A larger context window is not treated as a replacement for retrieval quality. RTDC's preferred architecture is hierarchical: persistent/atomic candidate retrieval narrows the corpus, verification checks retrieved evidence, and typed judgments decide or route. This keeps context bounded and provides explicit review gates. Very large-context models can be another input option, not an assumption that retrieval is solved.

## Legal and provenance boundary

Do not collect another decision service's outputs to train, distill, calibrate, tune, or benchmark this accelerator unless the operator has a documented right to that exact use. Development fixtures and evaluation data should be operator-owned, synthetic, openly licensed for the intended use, or otherwise documented as authorized.
