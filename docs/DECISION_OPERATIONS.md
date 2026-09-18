# Decision operations

ROOOMTECH Decision Core 0.5 adds higher-level, vendor-neutral decision primitives on top of the existing typed decision engine. They are independently implemented from general software patterns and do not reproduce a third-party SDK, prompt library, protocol, private API or service output.

## Endpoints

- `POST /v1/ops/detect` — probability that a property is present
- `POST /v1/ops/route` — choose the next code path with confidence gating
- `POST /v1/ops/score` — choose a rubric band and return an expected numeric score
- `POST /v1/ops/verify` — run multiple policy/quality checks over an artifact
- `POST /v1/ops/rank` — rank supplied candidates
- `POST /v1/ops/search` — alias of rank for retrieval-style workflows
- `POST /v1/ops/features` — turn text into probabilistic categorical features

The existing `/v1/decide` endpoint remains the general classification primitive.

## Detection

```json
{
  "input": "Please refund my order",
  "property": "refund request",
  "provider": "rules",
  "threshold": 0.8,
  "keywords": ["refund"]
}
```

Detection always returns the probability as well as the boolean threshold result. Low-confidence cases can be sent to review rather than forced into a definitive answer.

## Routing

Routes are explicit code paths. Each route can optionally include deterministic keywords, or a trained `model_id` / authorized OpenAI-compatible endpoint can make the semantic decision.

```json
{
  "input": "I cannot log in and need to reset my password",
  "provider": "rules",
  "routes": [
    {"id": "billing", "keywords": ["invoice", "charge"]},
    {"id": "account", "keywords": ["login", "password"]}
  ]
}
```

## Scoring

A rubric is represented as discrete bands with numeric values. The API returns both the most likely band and the probability-weighted expected score.

This is useful when an application needs a machine-readable severity, relevance, quality or priority value but still wants uncertainty preserved.

## Verification

Verification runs independent checks over the same artifact. Each check produces a violation probability and one of `pass`, `fail`, or `review`.

This can sit around model inputs, model outputs, tool-call arguments, generated documents, policies, code-review text or other artifacts. The caller owns the final policy and any automated action.

## Ranking and search

`local_ngram` is an offline Unicode character n-gram similarity method. It is fast and multilingual but lexical: it should not be represented as deep semantic understanding.

`openai_compatible` is an optional model-based reranker. It is used only when explicitly requested and when the deployer has configured an endpoint they are authorized to use.

## Probabilistic feature extraction

`/v1/ops/features` evaluates multiple categorical features in one request and returns the full probability vector for each feature. Those values can feed a downstream statistical/ML model without throwing away uncertainty.

## Examples of workflows

These primitives can be composed into customer-support triage, model routing, content checks, document review, candidate matching, product ranking, risk queues, retrieval, moderation, quality control, CI checks and other domain workflows. Domain-specific policies and regulated decisions should include appropriate human review and validation.

## Legal / product separation

Do not copy another vendor's prompts, private API behavior, benchmark datasets, service outputs, UI assets or brand terms into this project. Do not train or distill this project from a third-party hosted decision service unless a written license expressly permits it. Use independently collected or generated data and documented interfaces that you are authorized to access.
