# Website intelligence and semantic matrix

ROOOMTECH Decision Core v0.14 adds two independently designed capabilities for large-scale semantic workflows: a local many-to-many semantic matrix and a protected same-origin website audit.

These features are based on general information-retrieval, graph-analysis, crawling and similarity-scoring concepts. They do not reproduce another vendor's API protocol, console, prompt format, private implementation, model output, benchmark dataset or user interface.

## Semantic matrix

`POST /v1/ops/matrix`

The semantic matrix compares many left-side records against many right-side records and returns the strongest local matches for each left-side record. It uses sparse Unicode character n-gram features, deterministic hashing, cosine similarity and an inverted feature index. It makes no external model call.

Typical uses include:

- internal-link candidate generation;
- lead-to-account matching;
- candidate-to-role matching;
- product/catalog normalization;
- RAG candidate prefiltering;
- duplicate/near-duplicate discovery;
- support article recommendation;
- high-cardinality candidate generation before a more expensive decision stage.

The request has an explicit `max_logical_pairs` bound to avoid accidental quadratic workloads. The response distinguishes total logical pairs from candidate pairs that actually shared features and were scored.

Example:

```json
{
  "left": [{"id": "q1", "text": "エアコンが冷えない"}],
  "right": [
    {"id": "a1", "text": "冷房が効かないときの確認方法"},
    {"id": "a2", "text": "カード決済について"}
  ],
  "top_k_per_left": 5,
  "min_score": 0.1
}
```

## Website audit

`POST /v1/project/web/audit`

The audit crawls one public web origin, reconstructs the observed internal-link graph and proposes missing internal links using the local semantic matrix. It reports:

- pages discovered and fetched;
- HTML pages and failed pages;
- observed internal/external link counts;
- pages with no observed inbound internal link within the crawled graph;
- broken internal links for targets that were actually fetched and failed;
- missing internal-link suggestions;
- whether the proposed target title/H1 already appears literally in the source page text;
- logical semantic-pair count and candidate-pair count;
- total audit latency.

Fetched page bodies are held in memory for the request and are not persisted by this module. Response page summaries contain metadata and counts rather than full page bodies.

### Enable explicitly

Server-side fetching is disabled by default:

```bash
RTDC_WEB_INTELLIGENCE_ENABLED=true
```

Outside enterprise project-key mode, `RTDC_ADMIN_API_KEY` must also be configured. This prevents the endpoint from becoming an unauthenticated public fetch proxy.

Under enterprise project-key enforcement, use a project key with the dedicated `web` scope. The audit consumes project quota and writes metadata-only audit information; fetched page bodies are not copied into the enterprise audit table.

## Network safety boundary

The crawler is intentionally restrictive:

- only `http` and `https` are accepted;
- only ports 80 and 443 are accepted;
- URLs containing credentials are rejected;
- private, loopback, link-local, reserved and other non-public IP destinations are rejected;
- DNS answers containing non-public addresses are rejected;
- redirects are followed only within the original origin;
- discovered crawl links are restricted to the original origin;
- JavaScript is not executed;
- per-page bytes, total page count, concurrency and timeout are bounded;
- `robots.txt` is respected by default;
- query strings are ignored by default to reduce crawl explosions.

These controls reduce SSRF and crawl-abuse risk, but production deployments should still apply egress firewall rules, DNS controls, edge rate limits and organizational authorization. Do not use the crawler against sites you are not permitted to crawl.

## Interpretation limits

The internal-link recommender is a local candidate-generation signal, not an SEO ranking guarantee. A suggested link can be semantically related while still being editorially inappropriate. The operator should review recommendations before publishing changes.

The crawler does not execute JavaScript, render SPAs, inspect Search Console data, estimate search-engine rankings, or modify a site. Those can be added through separately authorized connectors or controlled browser/rendering infrastructure later.

## Independent-development boundary

Do not use third-party proprietary decision-service outputs as training labels or distillation targets for this feature. Do not copy another product's page layouts, prompts, benchmark claims or private crawler behavior. Competitive research may be used only to identify broad customer problems such as high-volume semantic matching, website graph analysis and cost-sensitive automation.
