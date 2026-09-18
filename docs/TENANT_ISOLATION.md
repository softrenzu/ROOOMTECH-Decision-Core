# Project tenant isolation

ROOOMTECH Decision Core v0.13 adds a project authorization boundary for persisted datasets, human-review items and local models. The goal is that a project credential cannot read, mutate, train from, export, invoke or enumerate a resource owned by another project.

## Resource ownership registry

Each persisted tenant resource is bound to exactly one enterprise project in the `project_resources` registry:

- `dataset`
- `review`
- `model`

A `(resource_type, resource_id)` can have only one project owner. Re-registering the same resource for the same project is idempotent; trying to claim it for another project is rejected.

Project-facing APIs always check ownership before accessing the underlying store. When the caller supplies another project's resource ID, the API returns a not-found response rather than exposing which project owns the resource.

The reference registry is stored in the enterprise SQLite database. The underlying dataset, review and model stores remain shared local stores, but project-facing access is mediated by the ownership registry. This is an application-level cross-project authorization boundary, not physical database-per-tenant isolation.

For production defense in depth, use managed storage with encryption, backups and database-level tenant controls such as row-level security, separate schemas or separate databases where appropriate.

## Project-scoped dataset API

Use a project key containing the `datasets` scope:

```text
POST   /v1/project/datasets
GET    /v1/project/datasets
GET    /v1/project/datasets/{dataset_id}
DELETE /v1/project/datasets/{dataset_id}
POST   /v1/project/datasets/{dataset_id}/examples
GET    /v1/project/datasets/{dataset_id}/examples
POST   /v1/project/datasets/{dataset_id}/import-reviews
POST   /v1/project/datasets/{dataset_id}/train
GET    /v1/project/datasets/{dataset_id}/models
```

Dataset training is allowed only after the dataset ownership check. A model created by project dataset training is automatically registered to the same project.

Review import only considers review items owned by the same project. Another project's resolved review cannot become training data through the project API.

## Project-scoped human review API

Use a project key containing the `reviews` scope:

```text
POST /v1/project/reviews
GET  /v1/project/reviews
GET  /v1/project/reviews/{review_id}
POST /v1/project/reviews/{review_id}/resolve
GET  /v1/project/reviews/export/training-examples
GET  /v1/project/active-learning/candidates
```

Lists are assembled from resource IDs owned by the authenticated project rather than filtering a global result after it has been returned to the caller.

Training-example export includes only the authenticated project's resolved reviews whose raw input was explicitly retained.

## Project-scoped model API

Use a project key containing the `models` scope:

```text
GET  /v1/project/models
GET  /v1/project/models/{model_id}
POST /v1/project/models/{model_id}/predict
```

Direct `POST /v1/models/{model_id}/predict` is also tenant-checked when enterprise project enforcement is enabled. General decision operations that specify a local `model_id` inherit the request tenant context, so another project's model ID cannot be used through `/v1/decide` or the higher-level decision-operation wrappers.

Administrative model promotion claims an unowned model for the target project, or confirms the existing ownership. A model already owned by another project cannot be promoted into a different project.

## WebSocket project authentication

When `RTDC_ENTERPRISE_ENFORCE_PROJECT_KEYS=true`, `/v1/realtime/ws` requires `X-RTDC-Project-Key` with the `realtime` scope.

Authentication is checked at connection time and again for every message. The per-message check means:

- daily request quota is consumed per WebSocket message;
- key revocation is observed by an already-open connection;
- key expiration is re-evaluated;
- project disablement is re-evaluated;
- tenant model context is applied while dispatching the message;
- each processed WebSocket message creates metadata-only audit information.

When enterprise project enforcement is disabled, the legacy `X-RTDC-API-Key` realtime behavior remains available.

## Management-plane hardening

When enterprise project enforcement is enabled, administrative routes fail closed if their management secret is not configured. `RTDC_ADMIN_API_KEY` is required for administrative/model/map-reduce/profile operations. Dataset, review and Studio management routes require `RTDC_STUDIO_API_KEY` or the admin key.

Project keys do not grant operator/admin access. Conversely, the operator/admin plane intentionally remains capable of maintenance and migration across projects. Tenant isolation is therefore a boundary between project credentials, not a claim that the service operator is cryptographically unable to access customer data.

## Migration note

Resources created before v0.13 may not yet have an ownership record. They should be deliberately adopted by the correct project before exposing them through project-facing APIs. The system does not guess ownership from names, paths or decision IDs.

## Current boundary

v0.13 provides hard application authorization checks for datasets, review items and local models plus project-authenticated WebSocket messages. It does not claim physical tenant isolation of the SQLite files, operating-system process isolation, or isolation of every administrator-created runtime object such as legacy realtime fast profiles. Production SaaS deployments should add storage-layer tenant controls and tenant ownership for any additional persisted resource type that is exposed to project users.
