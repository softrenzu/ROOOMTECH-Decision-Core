# Decision Graph Runtime

ROOOMTECH Decision Core v0.16 adds a project-scoped Decision Graph Runtime for composing small probabilistic decisions into a governed software workflow. The graph format and runtime are independently designed for RTDC; they do not reproduce a third-party SDK, prompt protocol, API schema, UI, output format, or private implementation.

## What it does

A graph is a bounded acyclic directed graph. Nodes can run RTDC operations, evaluate gates, create a Human Review item, or emit an action. Independent ready nodes execute concurrently up to `max_parallel`.

Supported node kinds:

- `decide`: normal typed RTDC decision request
- `detect`: binary property detection
- `route`: semantic route selection
- `score`: ordered rubric scoring
- `verify`: one or more semantic checks
- `features`: probabilistic feature extraction
- `extract`: JSON-Schema structured extraction
- `gate`: deterministic conditions over prior node outputs, input or context
- `review`: enqueue a Human Review item
- `action`: `emit` or allowlisted HTTPS webhook

The runtime supports conditional edges, per-node timeout, bounded retry, graph-level timeout, error policy (`fail`, `skip`, `review`), dry-run, versioned definitions, metadata traces, explicit replay, project isolation and quota/audit integration.

## API

```text
POST /v1/project/graphs/validate
POST /v1/project/graphs
GET  /v1/project/graphs
GET  /v1/project/graphs/{graph_id}?version=N
POST /v1/project/graphs/{graph_id}/run
GET  /v1/project/graphs/runs/{run_id}
POST /v1/project/graphs/runs/{run_id}/replay?dry_run=true
```

Under enterprise enforcement, use a Project Key with the `graphs` scope. Outside enterprise mode, `RTDC_GRAPH_API_KEY` is used when configured and otherwise falls back to `RTDC_ADMIN_API_KEY`.

## Example

```json
{
  "graph_id": "support_refund",
  "name": "Support refund flow",
  "nodes": [
    {
      "id": "refund",
      "kind": "detect",
      "config": {
        "property": "refund request",
        "provider": "rules",
        "threshold": 0.75,
        "review_below": 0.65,
        "keywords": ["返金", "refund"]
      }
    },
    {
      "id": "risk",
      "kind": "score",
      "config": {
        "criterion": "refund risk",
        "provider": "rules",
        "bands": [
          {"id": "low", "value": 0, "keywords": ["少額"]},
          {"id": "high", "value": 1, "keywords": ["高額", "不正"]}
        ]
      }
    },
    {
      "id": "gate",
      "kind": "gate",
      "config": {
        "mode": "all",
        "conditions": [
          {"path": "nodes.refund.detected", "op": "eq", "value": true},
          {"path": "nodes.risk.expected_score", "op": "gte", "value": 0.5}
        ]
      }
    },
    {
      "id": "human",
      "kind": "review",
      "config": {
        "decision_id": "refund_risk_review",
        "confidence_path": "nodes.refund.probability",
        "include_paths": ["nodes.refund", "nodes.risk"],
        "store_input": false
      }
    }
  ],
  "edges": [
    {"source": "refund", "target": "gate"},
    {"source": "risk", "target": "gate"},
    {
      "source": "gate",
      "target": "human",
      "condition": {"path": "nodes.gate.passed", "op": "eq", "value": true}
    }
  ]
}
```

`refund` and `risk` are both entry nodes, so the runtime can execute them in parallel. `gate` waits for both because it has incoming edges from both. `human` runs only when the gate passes.

## Paths and templates

Conditions and templates use an RTDC path syntax:

```text
input
context.customer_id
nodes.refund.probability
nodes.route.route
nodes.verify.findings.0.status
```

A string beginning with `$` inside an action payload is replaced with the value at that path:

```json
{
  "action": "emit",
  "payload": {
    "customer_id": "$context.customer_id",
    "risk": "$nodes.risk.expected_score"
  }
}
```

## Human Review

A `review` node writes to the existing RTDC Human Review Queue. Raw input retention remains off by default. When enterprise tenant isolation is active, the created review item is registered to the authenticated project.

## Action safety

`emit` is an RTDC internal action result. It does not make a network request.

External webhook execution is deliberately fail-closed:

```bash
RTDC_GRAPH_ACTIONS_ENABLED=true
RTDC_GRAPH_WEBHOOK_HOSTS=hooks.example.com,workflow.example.net
```

A webhook must use HTTPS on port 443, have no URL credentials, match the exact host allowlist and resolve only to public addresses. Redirects are not followed. RTDC sends an `Idempotency-Key` header plus an `X-RTDC-Graph-Run` identifier. Optional bearer credentials are referenced only by environment-variable name through `config.auth_env`; the secret value is not stored in the graph database.

Graph retries cannot force a third-party receiver to implement idempotency. For irreversible operations, the receiver must enforce idempotency/transaction semantics and normal authorization itself.

## Dry run

`dry_run` defaults to `true`. Semantic nodes and gates execute, while external actions are only simulated. This is the recommended mode for validation, regression tests and replay.

## Trace and privacy

Run input is not retained unless the caller explicitly sets `retain_input: true`. The store always keeps a SHA-256 input digest for correlation. Node outputs are returned to the current caller, but persisted run output/trace payloads are stripped by default. Set `persist_output_data: true` in a graph definition only when the operator has an appropriate data-retention policy.

Replay requires a run whose input was explicitly retained. Replay defaults back to dry-run to avoid accidental repeated side effects.

## Limits

A graph can contain at most 100 nodes and 500 edges. It must be acyclic. `max_parallel` is capped at 64, individual node timeouts are capped at 120 seconds, and total graph runtime is capped at 15 minutes. These are application limits, not resource-isolation guarantees.

For production SaaS, use a managed transactional graph/run store, encrypted data at rest, distributed leases for horizontally scaled workers, and a dedicated outbound egress policy in addition to the application-layer URL checks.
