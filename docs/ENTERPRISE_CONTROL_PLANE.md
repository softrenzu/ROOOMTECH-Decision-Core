# Enterprise control plane

ROOOMTECH Decision Core v0.13 extends the reference control plane with project-scoped credentials, usage limits, metadata-only audit trails, controlled local-model promotion/rollback, project ownership for datasets/reviews/models, and project-authenticated WebSocket traffic.

The bundled implementation uses SQLite and is intended for local, evaluation, and single-node deployments. It demonstrates the product contract and authorization model; it is not a substitute for a highly available identity platform, enterprise secret manager, centralized policy engine, SIEM, or managed database.

## Enable project-key enforcement

Project-key enforcement is off by default so existing local deployments keep their current behavior.

```bash
RTDC_ADMIN_API_KEY=<admin-secret>
RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS=true
RTDC_ENTERPRISE_DB=data/enterprise.sqlite3
RTDC_ENTERPRISE_KEY_PEPPER=<stable-secret-pepper>
```

When enabled, ordinary HTTP inference traffic under `/v1/` requires `X-RTDC-Project-Key`. Management endpoints continue to use their admin/studio controls and fail closed when the required management secret is not configured.

The principal scopes are:

- `inference` for general decision inference;
- `guardrails` for `/v1/guardrails/*`;
- `realtime` for realtime HTTP/WebSocket inference;
- `datasets` for project dataset management;
- `reviews` for project review/active-learning management;
- `models` for project model inspection and direct prediction.

## Projects

```text
POST  /v1/admin/projects
GET   /v1/admin/projects
GET   /v1/admin/projects/{project_id}
PATCH /v1/admin/projects/{project_id}
```

A project has an enabled/disabled state and a daily request quota. Disabling a project immediately prevents its project keys from authenticating, including already-open WebSocket connections on their next message.

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

When project-key enforcement is active, authenticated inference requests atomically increment the project's daily request counter. Project model prediction and WebSocket messages also consume the quota. Requests above the configured quota receive HTTP 429; an over-quota WebSocket connection is closed after an error response.

This is a product-level quota, not a precise billing meter. High-scale or multi-node deployments should move counters to a centralized datastore and define billing/event-delivery semantics appropriate to the service.

## Audit trail

```text
GET /v1/admin/audit
```

For authenticated project inference requests the reference implementation stores:

- timestamp;
- project and key IDs;
- HTTP method/path or WebSocket path;
- response/result status code;
- server-side latency;
- request correlation ID.

It intentionally does not store request bodies, prompts, model outputs, uploaded media, citations, or tool arguments. This reduces unnecessary data retention but also means the audit record is operational metadata rather than a full evidentiary transcript.

## Local-model promotion and rollback

```text
POST /v1/admin/projects/{project_id}/models/promote
GET  /v1/admin/projects/{project_id}/models
POST /v1/admin/projects/{project_id}/models/rollback
GET  /v1/project/deployments
POST /v1/project/predict
```

Promotion verifies that the referenced local model exists and that its `decision_id` matches the requested deployment slot. With the v0.13 tenant resource registry installed, promotion also claims an unowned model for the target project or confirms existing ownership. A model already owned by another project cannot be promoted into a different project.

Each promotion increments a version and archives the prior deployment. Rollback promotes the most recent historical model as a new deployment version, keeping an append-style deployment history.

`POST /v1/project/predict` resolves the authenticated project's promoted model without requiring the client to supply a model ID.

## Tenant resource isolation

v0.13 introduces the `project_resources` ownership registry for persisted `dataset`, `review`, and `model` resources. Each resource ID can belong to only one project.

Project-facing APIs check ownership before reading, mutating, training from, exporting or invoking a resource. A caller that supplies another project's resource ID receives a not-found response rather than ownership information.

Project dataset training automatically registers the resulting model to the same project. Review-to-dataset import only considers reviews owned by that project. General decision requests that specify a local `model_id` run with the authenticated tenant context and cannot use another project's registered model.

The underlying reference SQLite dataset/review/model stores are still shared. The v0.13 boundary is therefore a hard application authorization boundary between project credentials, not physical database-per-tenant isolation. For production defense in depth, add managed encrypted storage and database-level row security, schema isolation or separate databases where appropriate.

See `docs/TENANT_ISOLATION.md`.

## WebSocket project authentication

When `RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS=true`, `/v1/realtime/ws` requires `X-RTDC-Project-Key` with the `realtime` scope. The project key is checked at connection time and again before every message is dispatched.

The per-message check enforces quota consumption, revocation, expiration and project disablement on long-lived connections. The tenant context is active during dispatch and each processed message is recorded in the metadata-only audit log.

When enterprise enforcement is disabled, the legacy `X-RTDC-API-Key` path remains available for trusted/local deployments.

## Migration and operator boundary

Resources created before v0.13 can be unowned. They must be deliberately adopted by the correct project before project-facing APIs expose them; ownership is never guessed from a name, path or decision ID.

The service operator/admin plane intentionally has cross-project maintenance capability. Tenant isolation protects project credentials from other projects; it is not a claim that the infrastructure operator is cryptographically unable to access customer resources.

Legacy realtime fast profiles are not yet project-owned resources. If they are exposed in a multi-tenant SaaS deployment, add profile ownership or isolate them at the deployment edge before treating them as tenant-private objects.

## Security notes

- Keep `RTDC_ADMIN_API_KEY`, the enterprise pepper, signing keys, and project keys outside source control.
- Terminate TLS before exposing any project key over a network.
- Put administrative endpoints behind network and identity controls in addition to API keys.
- Use a secret manager for production credentials.
- Apply rate limiting and abuse controls at the edge as well as application quotas.
- Send audit events to an approved centralized log/SIEM for production use.
- Treat scopes as application authorization signals, not a replacement for infrastructure IAM.
- Add storage-layer tenant controls for production SaaS as defense in depth.

## Independent development

This control plane uses generic project/API-key/quota/audit/deployment/tenant-ownership concepts and ROOOMTECH-owned data models. It does not reproduce another vendor's private administration API, UI, entitlement system, billing implementation, or internal tenant architecture.
