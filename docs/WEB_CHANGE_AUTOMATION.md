# Governed website change automation

ROOOMTECH Decision Core v0.15 extends Website Intelligence from detection into a governed change workflow. The design is intentionally conservative: scheduled audits may discover and stage changes, but only an explicit authenticated review action may publish a WordPress edit.

## Workflow

1. Register a WordPress connector. The connector stores the site URL and the *names* of two environment variables containing the WordPress username and application password. Secret values are not written to the RTDC web-change database.
2. Run a staged audit manually or on a schedule. The existing same-origin crawler finds internal-link opportunities.
3. RTDC maps source URLs to editable WordPress posts/pages through the standard WordPress REST API with `context=edit`.
4. For each suggestion, RTDC creates a project-owned proposal. If it can find the suggested anchor as one exact unlinked text node in raw WordPress content, it stores a short before/after preview and a SHA-256 snapshot of the source content.
5. A reviewer can approve, reject, or explicitly use the review-then-one-click apply endpoint/UI.
6. Immediately before writing, RTDC re-fetches the source object and compares its SHA-256 hash with the reviewed snapshot. If the page changed, the proposal becomes `stale` and is not written.
7. WordPress is updated only when the proposal is approved, marked auto-applicable, the source snapshot still matches, and the exact safe insertion point still exists.

## Enable

```bash
RTDC_WEB_INTELLIGENCE_ENABLED=true
RTDC_WEB_CHANGE_DB=data/web_changes.sqlite3
```

Outside enterprise project-key mode, `RTDC_ADMIN_API_KEY` is also required. Under enterprise enforcement, use a project key with the `web` scope.

To enable the interval scheduler:

```bash
RTDC_WEB_SCHEDULER_ENABLED=true
RTDC_WEB_SCHEDULER_POLL_SECONDS=60
```

Schedules never call an apply endpoint. They only run audits and create pending review proposals.

## WordPress credentials

Create application-password credentials in WordPress and expose them to the RTDC process through environment variables. A connector stores only the environment-variable names.

Example runtime configuration:

```bash
WP_SITE_USER=editor@example.com
WP_SITE_APP_PASSWORD='xxxx xxxx xxxx xxxx xxxx xxxx'
```

Example connector request:

```json
{
  "name": "Main site",
  "site_url": "https://example.com",
  "username_env": "WP_SITE_USER",
  "application_password_env": "WP_SITE_APP_PASSWORD"
}
```

The current reference implementation supports WordPress `pages` and `posts`. It does not store raw WordPress credentials, and it does not expose an arbitrary URL write endpoint.

## API

```text
POST   /v1/project/web/connectors/wordpress
GET    /v1/project/web/connectors
POST   /v1/project/web/connectors/{connector_id}/test
POST   /v1/project/web/audits/stage
GET    /v1/project/web/audit-runs
GET    /v1/project/web/proposals
GET    /v1/project/web/proposals/{proposal_id}
POST   /v1/project/web/proposals/{proposal_id}/approve
POST   /v1/project/web/proposals/{proposal_id}/reject
POST   /v1/project/web/proposals/{proposal_id}/apply
POST   /v1/project/web/proposals/{proposal_id}/approve-apply
POST   /v1/project/web/schedules
GET    /v1/project/web/schedules
DELETE /v1/project/web/schedules/{schedule_id}
GET    /web-review
```

`/web-review` is a lightweight ROOOMTECH-designed review surface. It contains no tenant data itself; protected API calls still require the project/admin credential supplied by the operator.

## Proposal states

- `pending`: staged and waiting for review.
- `approved`: reviewed and allowed to proceed to apply.
- `rejected`: reviewer rejected the proposal.
- `applied`: WordPress accepted the explicit update.
- `stale`: source content or the safe insertion point changed after review; re-audit is required.
- `failed`: an authenticated WordPress operation failed.

A proposal may be reviewable but not `auto_applicable`. RTDC deliberately refuses automatic publishing in that case. Typical reasons include inability to map the source URL to a WordPress post/page, missing `content.raw`, or no exact unlinked text-node insertion point.

## Safety and concurrency

The write path reuses the Website Intelligence public-network restrictions and additionally pins WordPress REST requests to the configured site origin. The source revision hash provides optimistic concurrency control so a reviewed edit cannot silently overwrite intervening page changes.

The HTML edit is intentionally narrow. It inserts a single `<a>` only when the anchor appears as an exact contiguous ordinary text node. It skips text inside existing links, scripts, styles, code, `pre`, and `textarea` elements. It does not attempt broad HTML rewriting or AI-generated replacement text.

Scheduled jobs use atomic SQLite schedule claiming to reduce duplicate execution when several processes share the same database. The bundled SQLite store remains a reference single-node/small-deployment implementation; production multi-node SaaS should use a managed transactional datastore and distributed scheduler/lease mechanism.

## Data retention

The web-change database stores connector metadata, audit-run metadata, link proposals, short before/after snippets, source revision hashes, and schedule state. It does not store WordPress passwords or full fetched page bodies. WordPress raw content is processed transiently for proposal generation and pre-write validation.

## Rollback

RTDC v0.15 does not retain a full copy of the pre-change WordPress document, intentionally avoiding unnecessary content duplication. Operators should keep WordPress revisions/backups enabled. A future connector can expose revision-based rollback without storing an extra full copy inside RTDC.

## Independent-development boundary

This feature uses general crawler, REST connector, optimistic-concurrency, approval-workflow and scheduling patterns. It does not reproduce a third-party decision product's private code, prompts, UI, API schema, outputs or benchmark dataset. WordPress integration uses the standard WordPress REST API and operator-provided credentials.
