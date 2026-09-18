from __future__ import annotations

import json
import math
import os
import time
import uuid
from typing import Any

from app.advanced_models import MapItemResult, MapReduceJobStatus, MapReduceJobSubmission, MapReduceRequest, ReduceSpec


class RedisDistributedMapReduce:
    def __init__(self, engine):
        self.engine = engine
        self.url = os.getenv("RTDC_REDIS_URL", "").strip()
        self.queue_name = os.getenv("RTDC_MAPREDUCE_QUEUE", "rtdc:mapreduce:tasks")
        self.ttl = int(os.getenv("RTDC_MAPREDUCE_JOB_TTL_SECONDS", "3600"))
        self.shard_size = max(1, int(os.getenv("RTDC_MAPREDUCE_SHARD_SIZE", "250")))

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _redis(self):
        if not self.configured:
            raise RuntimeError("RTDC_REDIS_URL is not configured")
        try:
            import redis.asyncio as redis
        except ImportError as exc:
            raise RuntimeError("install the 'distributed' extra to use Redis workers") from exc
        return redis.from_url(self.url, decode_responses=True)

    def _job_key(self, job_id: str) -> str:
        return f"rtdc:mapreduce:job:{job_id}"

    async def submit(self, request: MapReduceRequest) -> MapReduceJobSubmission:
        client = self._redis()
        job_id = uuid.uuid4().hex
        total_shards = math.ceil(len(request.items) / self.shard_size)
        key = self._job_key(job_id)
        pipe = client.pipeline()
        pipe.hset(key, mapping={
            "state": "queued",
            "total_shards": total_shards,
            "completed_shards": 0,
            "total_items": len(request.items),
            "reduce": request.reduce.model_dump_json(),
            "include_map_results": "1" if request.include_map_results else "0",
            "created_at": str(time.time()),
        })
        pipe.expire(key, self.ttl)
        for shard_index, start in enumerate(range(0, len(request.items), self.shard_size)):
            shard_request = request.model_copy(update={
                "items": request.items[start : start + self.shard_size],
                "reduce": ReduceSpec(kind="collect"),
                "include_map_results": True,
            })
            task = {
                "job_id": job_id,
                "shard_index": shard_index,
                "offset": start,
                "request": shard_request.model_dump(mode="json", by_alias=True),
            }
            pipe.rpush(self.queue_name, json.dumps(task, ensure_ascii=False))
        await pipe.execute()
        return MapReduceJobSubmission(job_id=job_id, state="queued", total_shards=total_shards, total_items=len(request.items))

    async def status(self, job_id: str) -> MapReduceJobStatus:
        client = self._redis()
        key = self._job_key(job_id)
        meta = await client.hgetall(key)
        if not meta:
            raise FileNotFoundError(f"map/reduce job not found: {job_id}")
        result_text = await client.get(f"{key}:result")
        return MapReduceJobStatus(
            job_id=job_id,
            state=meta.get("state", "unknown"),
            total_shards=int(meta.get("total_shards", 0)),
            completed_shards=int(meta.get("completed_shards", 0)),
            total_items=int(meta.get("total_items", 0)),
            result=json.loads(result_text) if result_text else None,
            error=meta.get("error") or None,
        )

    async def _complete_shard(self, client, task: dict[str, Any], results: list[MapItemResult]):
        job_id = task["job_id"]
        key = self._job_key(job_id)
        shard_key = f"{key}:shard:{task['shard_index']}"
        await client.set(shard_key, json.dumps([item.model_dump(mode="json") for item in results], ensure_ascii=False), ex=self.ttl)
        completed = await client.hincrby(key, "completed_shards", 1)
        await client.hset(key, "state", "running")
        meta = await client.hgetall(key)
        if completed != int(meta.get("total_shards", 0)):
            return
        combined: list[MapItemResult] = []
        for shard_index in range(int(meta["total_shards"])):
            text = await client.get(f"{key}:shard:{shard_index}")
            if text:
                combined.extend(MapItemResult.model_validate(item) for item in json.loads(text))
        combined.sort(key=lambda item: item.index)
        reduce_spec = ReduceSpec.model_validate_json(meta["reduce"])
        reduced = self.engine.reduce_results(combined, reduce_spec)
        include = meta.get("include_map_results") == "1"
        final = {
            "items": int(meta.get("total_items", len(combined))),
            "ok": sum(1 for item in combined if item.ok),
            "failed": sum(1 for item in combined if not item.ok),
            "reduced": reduced,
            "results": [item.model_dump(mode="json") for item in combined] if include else [],
        }
        await client.set(f"{key}:result", json.dumps(final, ensure_ascii=False), ex=self.ttl)
        await client.hset(key, "state", "completed")
        await client.expire(key, self.ttl)

    async def worker_loop(self):
        client = self._redis()
        while True:
            popped = await client.blpop(self.queue_name, timeout=5)
            if not popped:
                continue
            _, raw = popped
            task = json.loads(raw)
            request = MapReduceRequest.model_validate(task["request"])
            offset = int(task["offset"])
            try:
                response = await self.engine.run(request)
                results = [item.model_copy(update={"index": item.index + offset}) for item in response.results]
            except Exception as exc:
                results = [
                    MapItemResult(index=offset + index, ok=False, error=f"shard failure: {type(exc).__name__}: {exc}")
                    for index in range(len(request.items))
                ]
            await self._complete_shard(client, task, results)
