from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from app.advanced_models import RealtimeBatchRequest, RealtimeEvent, RealtimeResult, SchemaExtractionRequest
from app.models import DecisionRequest
from app.operation_models import DetectionRequest, FeatureExtractionRequest, RouteRequest, ScoreRequest, VerificationRequest


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


class RealtimeDispatcher:
    def __init__(self, decision_engine, operations_engine, schema_extractor):
        self.decision_engine = decision_engine
        self.operations_engine = operations_engine
        self.schema_extractor = schema_extractor

    async def dispatch(self, event: RealtimeEvent) -> RealtimeResult:
        started = time.perf_counter()
        try:
            if event.kind == "ping":
                data = {"pong": True}
            elif event.kind == "decide":
                data = _dump(await self.decision_engine.decide(DecisionRequest.model_validate(event.request)))
            elif event.kind == "detect":
                data = _dump(await self.operations_engine.detect(DetectionRequest.model_validate(event.request)))
            elif event.kind == "route":
                data = _dump(await self.operations_engine.route(RouteRequest.model_validate(event.request)))
            elif event.kind == "score":
                data = _dump(await self.operations_engine.score(ScoreRequest.model_validate(event.request)))
            elif event.kind == "verify":
                data = _dump(await self.operations_engine.verify(VerificationRequest.model_validate(event.request)))
            elif event.kind == "features":
                data = _dump(await self.operations_engine.extract_features(FeatureExtractionRequest.model_validate(event.request)))
            elif event.kind == "extract":
                data = _dump(await self.schema_extractor.extract(SchemaExtractionRequest.model_validate(event.request)))
            else:
                raise ValueError(f"unsupported realtime event: {event.kind}")
            return RealtimeResult(
                request_id=event.request_id,
                kind=event.kind,
                ok=True,
                data=data,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
        except Exception as exc:
            return RealtimeResult(
                request_id=event.request_id,
                kind=event.kind,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )

    async def stream_batch(self, request: RealtimeBatchRequest) -> AsyncIterator[RealtimeResult]:
        semaphore = asyncio.Semaphore(request.concurrency)
        queue: asyncio.Queue[RealtimeResult] = asyncio.Queue()

        async def one(event: RealtimeEvent):
            async with semaphore:
                await queue.put(await self.dispatch(event))

        tasks = [asyncio.create_task(one(event)) for event in request.events]
        for _ in tasks:
            yield await queue.get()
        await asyncio.gather(*tasks)
