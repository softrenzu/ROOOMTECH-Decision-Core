# Guardrail gateway, tool-call gate, and RAG screening

ROOOMTECH Decision Core v0.11 adds an independently designed policy gateway for screening AI inputs, outputs, tool calls, and cited RAG context before downstream software acts on them.

This component is a decision-support control. It is not a security guarantee, compliance certification, malware scanner, DLP replacement, factuality oracle, or proof that an AI response is safe or correct.

## Independent-development boundary

The guardrail implementation is based on generic software-security and policy-enforcement concepts. It does not require another vendor's prompts, private rule sets, service outputs, internal schemas, SDK implementation, benchmark set, or proprietary model behavior.

Do not import a competitor's proprietary guardrail outputs as labels for training or tuning unless written terms expressly authorize that exact use. Custom policies should be written from the operator's own requirements, laws, contracts, security standards, and domain-specific risk definitions.

## API

```text
POST /v1/guardrails/evaluate
POST /v1/guardrails/tool-call
POST /v1/guardrails/rag
```

Set `RTDC_GUARDRAIL_API_KEY` for protected deployments. When blank, the gateway falls back to `RTDC_REALTIME_API_KEY`, then `RTDC_ADMIN_API_KEY`.

The endpoints do not persist submitted text or tool arguments by default.

## Input/output policy screening

`POST /v1/guardrails/evaluate` can inspect an input message, a model output, a proposed tool call, or any combination of the three.

Built-in local screening currently includes:

- prompt-injection-like instruction patterns in English and Japanese;
- email-, phone-, and long payment-card-like numeric patterns;
- credential-like strings such as bearer tokens, API-key assignments, and common secret-key forms;
- risky tool-action words such as deletion, transfer, payment, purchase, refund, shell/exec, and corresponding Japanese terms.

These are deterministic screening rules. Pattern matches can have false positives and false negatives. The response therefore reports findings and one of `allow`, `review`, or `block` instead of claiming certainty.

Operators can add independent custom policies with keywords and/or a semantic property. Semantic policies reuse RTDC's existing detection providers and can use a local classifier or an explicitly configured external provider that the operator is authorized to call.

## Tool-call gate

`POST /v1/guardrails/tool-call` evaluates a proposed action before an agent or application executes it.

It supports:

- explicit allowlists and blocklists for tool names;
- operator-defined policy rules;
- risky-action screening;
- a `user_authorized` flag for actions that require explicit authorization.

When a risky action is detected and explicit authorization is required but not present, the current default is `block`. Applications should still implement their own authorization, authentication, transactional limits, idempotency, approval, and rollback controls. The gateway is an additional decision layer, not a substitute for those controls.

## RAG citation/context screening

`POST /v1/guardrails/rag` accepts an answer, cited passages, and explicit claims. For each claim it computes a local Unicode character-n-gram lexical-support score against only the cited passages. It also scans retrieved context for prompt-injection-like patterns.

A claim is marked `pass`, `review`, or `fail` from configurable support thresholds. Any non-pass claim or detected context injection causes `requires_review=true`.

Lexical overlap is intentionally a fast first-pass filter. It does not prove entailment, factual correctness, absence of contradiction, or source authority. High-stakes deployments should combine this filter with independently defined semantic verification, trusted-source controls, and human review where appropriate.

## Recommended production flow

```text
User / external data
        |
        v
Input policy screen
        |
        v
LLM / agent / RAG
        |
        +--> cited context screen
        |
        v
Output policy screen
        |
        v
Proposed tool call
        |
        v
Tool-call gate
   /     |      \
allow  review   block
        |
        v
Human / application approval
```

## Privacy and logging

Guardrail API requests are not persisted by this module. If an application records findings, prompts, outputs, citations, or tool arguments elsewhere, that application is responsible for retention, access controls, personal-data handling, and disclosure obligations.

For enterprise deployment, log rule IDs, actions, confidence/probability where applicable, model/version identifiers, request correlation IDs, and approval outcomes without retaining more raw content than necessary.
