from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from pydantic import BaseModel

from app.advanced_models import MapItemResult, MapReduceRequest, MapReduceResponse, ReduceSpec, SchemaExtractionRequest
from app.models import DecisionRequest
from app.operation_models import DetectionRequest, FeatureExtractionRequest, RouteRequest, ScoreRequest, VerificationRequest


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _get_path(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


class MapReduceEngine:
    def __init__(self, decision_engine, operations_engine, schema_extractor):
        self.decision_engine = decision_engine
        self.operations_engine = operations_engine
        self.schema_extractor = schema_extractor

    @staticmethod
    def _input_text(item: Any, path: str) -> str:
        if isinstance(item, str):
            return item
        if path == "$":
            return json.dumps(item, ensure_ascii=False)
        try:
            value = _get_path(item, path)
        except KeyError:
            return json.dumps(item, ensure_ascii=False)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    async def _map_one(self, item: Any, request: MapReduceRequest) -> Any:
        text = self._input_text(item, request.map.input_field)
        config = dict(request.map.config)
        kind = request.map.kind
        if kind == "decide":
            return _jsonable(await self.decision_engine.decide(DecisionRequest(input=text, **config)))
        if kind == "detect":
            return _jsonable(await self.operations_engine.detect(DetectionRequest(input=text, **config)))
        if kind == "route":
            return _jsonable(await self.operations_engine.route(RouteRequest(input=text, **config)))
        if kind == "score":
            return _jsonable(await self.operations_engine.score(ScoreRequest(input=text, **config)))
        if kind == "verify":
            return _jsonable(await self.operations_engine.verify(VerificationRequest(artifact=text, **config)))
        if kind == "features":
            return _jsonable(await self.operations_engine.extract_features(FeatureExtractionRequest(input=text, **config)))
        if kind == "extract":
            return _jsonable(await self.schema_extractor.extract(SchemaExtractionRequest(input=text, **config)))
        raise ValueError(f"unsupported map operation: {kind}")

    async def _run_item(self, index: int, item: Any, request: MapReduceRequest, semaphore: asyncio.Semaphore) -> MapItemResult:
        async with semaphore:
            try:
                value = await self._map_one(item, request)
                return MapItemResult(index=index, ok=True, value=value)
            except Exception as exc:
                if request.fail_fast:
                    raise
                return MapItemResult(index=index, ok=False, error=f"{type(exc).__name__}: {exc}")

    def reduce_results(self, results: list[MapItemResult], spec: ReduceSpec) -> Any:
        successful = [item for item in results if item.ok]
        if spec.kind == "collect":
            return [item.value for item in successful]
        values: list[tuple[MapItemResult, Any]] = []
        for item in successful:
            try:
                values.append((item, _get_path(item.value, spec.path)))
            except KeyError:
                continue
        if spec.kind == "count_by":
            counts: dict[str, int] = {}
            for _, value in values:
                key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
                counts[key] = counts.get(key, 0) + 1
            return counts
        numeric = [(item, float(value)) for item, value in values]
        if spec.kind == "sum":
            return sum(value for _, value in numeric)
        if spec.kind == "avg":
            return (sum(value for _, value in numeric) / len(numeric)) if numeric else None
        if spec.kind == "min":
            return min((value for _, value in numeric), default=None)
        if spec.kind == "max":
            return max((value for _, value in numeric), default=None)
        if spec.kind == "top_k":
            numeric.sort(key=lambda pair: pair[1], reverse=True)
            return [
                {"index": item.index, "score": value, "value": item.value}
                for item, value in numeric[: spec.top_k]
            ]
        raise ValueError(f"unsupported reducer: {spec.kind}")

    async def run(self, request: MapReduceRequest) -> MapReduceResponse:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(request.concurrency)
        tasks = [self._run_item(index, item, request, semaphore) for index, item in enumerate(request.items)]
        results = await asyncio.gather(*tasks)
        reduced = self.reduce_results(results, request.reduce)
        ok = sum(1 for item in results if item.ok)
        return MapReduceResponse(
            items=len(results),
            ok=ok,
            failed=len(results) - ok,
            reduced=reduced,
            results=results if request.include_map_results else [],
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )

    async def stream(self, request: MapReduceRequest) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(request.concurrency)
        queue: asyncio.Queue[MapItemResult] = asyncio.Queue()

        async def worker(index: int, item: Any):
            result = await self._run_item(index, item, request, semaphore)
            await queue.put(result)

        tasks = [asyncio.create_task(worker(index, item)) for index, item in enumerate(request.items)]
        results: list[MapItemResult] = []
        for _ in tasks:
            result = await queue.get()
            results.append(result)
            yield {"type": "map", "data": result.model_dump(mode="json")}
        await asyncio.gather(*tasks)
        results.sort(key=lambda item: item.index)
        reduced = self.reduce_results(results, request.reduce)
        yield {
            "type": "reduce",
            "data": {
                "items": len(results),
                "ok": sum(1 for item in results if item.ok),
                "failed": sum(1 for item in results if not item.ok),
                "reduced": reduced,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }
