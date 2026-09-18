# Structured extraction, Map/Reduce and realtime APIs

Version 0.6 adds three independent platform capabilities.

## Arbitrary JSON Schema extraction

`POST /v1/extract` accepts input text plus a JSON Schema. The result is validated before being returned. `provider=heuristic` handles JSON and key/value text locally. `provider=auto` uses the local heuristic first and calls the configured OpenAI-compatible model only when needed. `x-rtdc-aliases` can map localized field names to schema properties.

The implementation validates output with JSON Schema and does not expose chain-of-thought.

## Map/Reduce

`POST /v1/mapreduce/run` maps one Decision Core operation over up to 100,000 items with bounded asyncio concurrency and then reduces the results with `collect`, `count_by`, `sum`, `avg`, `min`, `max` or `top_k`.

`POST /v1/mapreduce/stream` emits NDJSON map results as soon as each item completes and then emits a final reduce event.

For horizontal workers, install `.[distributed]`, configure `RTDC_REDIS_URL`, submit with `POST /v1/mapreduce/jobs`, and run one or more workers with:

```bash
python -m app.mapreduce_worker
```

Jobs are split into shards. Redis stores submitted payloads and shard results temporarily until the configured TTL expires. Do not enable distributed mode for sensitive data unless your Redis deployment, retention policy and access controls are appropriate.

## Realtime streaming

`WS /v1/realtime/ws` accepts independent decision events and returns one result per message. Supported event kinds are `ping`, `decide`, `detect`, `route`, `score`, `verify`, `features` and `extract`.

`POST /v1/realtime/stream` accepts a batch of events and returns results as NDJSON as soon as each event completes. Results may arrive out of order; use `request_id` to correlate them.

Set `RTDC_REALTIME_API_KEY` in shared deployments and send it as `X-RTDC-API-Key` on the WebSocket handshake.

These transports do not make a model intrinsically faster; latency still depends on the selected local model, rules, external model and network path.
