# Enterprise control plane

ROOOMTECH Decision Core v0.12 adds a reference control plane for commercial deployments that need project-scoped credentials, usage limits, metadata-only audit trails, and controlled local-model promotion/rollback.

The bundled implementation uses SQLite and is intended for local, evaluation, and single-node deployments. It demonstrates the product contract and security model; it is not a substitute for a highly available identity platform, enterprise secret manager, centralized policy engine, SIEM, or managed database.

## Enable project-key enforcement

Project-key enforcement is off by default so existing local deployments keep their current behavior.

```bash
RTDC_ADMIN_API_KEY=<admin-secret>
RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS=true
RTDC_ENTERPRISE_DB=data/enterprise.sqlite3
RTDC_ENTERPRISE_KEY_PEPPER=<stable-secret-pepper>
```

When enabled, ordinary HTTP inference traffic under `/v1/` requires `X-RTDC-Project-Key`. Management endpoints continue to use their existing admin/studio controls.

The current scope mapping is:

- `guardrails` for `/v1/guardrails/*`;
- `realtime` for ordinary `/v1/realtime/*` inference routes;
- `inference` for other protected inference endpoints.

WebSocket authentication remains controlled by the existing realtime key path. Project-key enforcement for WebSockets should be added at the edge or in a future dedicated WebSocket tenant-auth layer before relying on project credentials alone for WebSocket multi-tenancy.

## Projects

```text
POST  /v1/admin/projects
GET   /v1/admin/projects
GET   /v1/admin/projects/{project_id}
PATCH /v1/admin/projects/{project_id}
```

A project has an enabled/disabled state and a daily request quota. Disabling a project immediately prevents its project keys from authenticating.

## Scoped API keys

```text
POST   /v1/admin/projects/{project_id}/keys
GET    /v1/admin/projects/{project_id}/keys
DELETE /v1/admin/projects/{project_id}/keys/{key_id}
```

Scopes are `inference`, `guardrails`, `realtime`, `datasets`, `reviews`, and `models`.

The raw key is returned only when it is issued. The database stores only a SHA-256 digest, or an HMAC-SHA-256 digest when `RTDC_ENTERPRISE_KEY_PEPPER` is configured. Production deployments should use a stable pepper kept in a secret manager. Rotating the pepper invalidates existing keys unless a migration strategy is implemented.

Keys can have an expiration date and can be revoked independently of the project.

## Daily request quota

When project-key enforcement is active, an authenticated inference request atomically increments the project's daily request counter. Requests above the configured quota receive HTTP 429.

This is a product-level quota, not a precise billing meter. High-scale or multi-node deployments should move counters to a centralized datastore and define billing/event-delivery semantics appropriate to the service.

## Audit trail

```text
GET /v1/admin/audit
```

For authenticated project inference requests the reference implementation stores:

- timestamp;
- project and key IDs;
- HTTP method and path;
- response status code;
- server-side middleware latency;
- request correlation ID.

It intentionally does not store request bodies, prompts, model outputs, uploaded media, citations, or tool arguments. This reduces unnecessary data retention but also means the audit record is operational metadata rather than a full evidentiary transcript.

## Local-model promotion and rollback

```text
POST /v1/admin/projects/{project_id}/models/promote
GET  /v1/admin/projects/{project_id}/models
POST /v1/admin/projects/{project_id}/models/rollback
GET  /v1/project/deployments
```

Promotion verifies that the referenced local model exists and that its `decision_id` matches the requested deployment slot. Each promotion increments a version and archives the prior deployment. Rollback promotes the most recent historical model as a new deployment version, keeping an append-style deployment history.

This control plane currently records deployment intent. Existing inference APIs still accept explicit model IDs; automatic resolution of a project/environment deployment alias into every inference request is a separate integration step and should not be assumed from the deployment record alone.

## Isolation boundary

Projects currently scope API credentials, request quotas, audit metadata, and deployment records. The v0.12 SQLite dataset/review/model stores are not yet physically partitioned by project. Therefore v0.12 should not be marketed as hard multi-tenant data isolation.

For a SaaS launch, add project ownership columns and authorization checks to datasets, reviews, models, benchmark results and stored artifacts; use managed encrypted storage; and test tenant-boundary failures explicitly.

## Security notes

- Keep `RTDC_ADMIN_API_KEY`, the enterprise pepper, signing keys, and project keys outside source control.
- Terminate TLS before exposing any project key over a network.
- Put administrative endpoints behind network and identity controls in addition to API keys.
- Use a secret manager for production credentials.
- Apply rate limiting and abuse controls at the edge as well as application quotas.
- Send audit events to an approved centralized log/SIEM for production use.
- Treat scopes as application authorization signals, not a replacement for infrastructure IAM.

## Independent development

This control plane uses generic project/API-key/quota/audit/deployment concepts and ROOOMTECH-owned data models. It does not reproduce another vendor's private administration API, UI, entitlement system, billing implementation, or internal tenant architecture.
