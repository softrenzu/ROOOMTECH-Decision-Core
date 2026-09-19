# Dynamic Candidate Decisions

ROOOMTECH Decision Core v0.17 adds high-cardinality candidate selection for cases where the valid choices change at request time. It is intended for product matching, lead/account matching, candidate-to-role matching, document routing, support-article selection and other workflows with hundreds to tens of thousands of candidates.

The implementation is independently designed. It does not reproduce a third-party SDK, protocol, prompt format, private API, benchmark dataset or output behavior.

## API

```text
POST /v1/project/candidates/select
```

Enterprise deployments use a project key with the `candidates` scope. Outside enterprise mode, configure `RTDC_CANDIDATE_API_KEY` or use the admin key.

## Two-stage selection

Stage 1 always runs locally. RTDC creates sparse Unicode character n-gram signatures and uses cosine similarity to reduce as many as 50,000 request-time candidates to a bounded shortlist.

Stage 2 is optional:

- `local_sparse`: use the local shortlist scores directly; no network call.
- `openai_compatible`: rerank only a small bounded subset through the configured OpenAI-compatible provider and blend that score with the local score.

The external rerank subset is capped at 50 candidates. This prevents an accidental request with tens of thousands of candidates from turning into tens of thousands of remote model evaluations.

## Example

```json
{
  "query": "新宿駅の近くでチェックイン方法を知りたい",
  "candidates": [
    {"id": "billing", "text": "請求書 支払い クレジットカード"},
    {"id": "checkin", "text": "新宿駅 宿泊施設 チェックイン 入室 方法"},
    {"id": "wifi", "text": "WiFi パスワード インターネット 接続"}
  ],
  "shortlist_k": 100,
  "final_k": 10,
  "final_method": "local_sparse",
  "min_selection_score": 0.15,
  "min_selection_margin": 0.02
}
```

The response returns `selected_id`, a ranked result list, local shortlist counts, optional rerank counts, a top-score `selection_signal`, the score margin between the top two candidates, and `requires_review`.

`selection_signal` is deliberately not described as a calibrated probability of correctness. It is a routing heuristic. Production automation should calibrate review thresholds on held-out, operator-owned examples for the target domain.

## Metadata filters

`metadata_equals` can remove ineligible candidates before semantic scoring. Typical examples are region, language, product family, tenant-specific category or availability state. Filtering is exact-match and deterministic.

## Resource bounds

A request is bounded to 50,000 candidates and a configurable total candidate-text budget. `shortlist_k` is capped at 500 and `final_k` at 100. The optional external rerank is capped at 50 candidates.

Candidate content is processed for the current request and is not persisted by the Dynamic Candidate engine.

## Privacy and legal boundary

The local stage does not send candidate text to an external provider. When `final_method=openai_compatible`, the query and bounded shortlist are sent to the operator-configured provider, so operators must ensure the provider and data use are authorized.

RTDC does not require or collect outputs from another proprietary decision service as training data. The feature uses general retrieval, filtering, shortlisting and reranking techniques implemented with RTDC-owned code.
