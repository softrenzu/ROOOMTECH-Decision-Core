from __future__ import annotations

import asyncio
import math
import time
from typing import Iterable

from app.performance_models import (
    FastDecisionRequest,
    LatencyStats,
    MapReduceLoadBenchmarkRequest,
    MapReduceLoadBenchmarkResponse,
    MapReduceRunMetric,
    RealtimeLoadBenchmarkRequest,
    RealtimeLoadBenchmarkResponse,
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _stats(
    values: Iterable[float],
    throughput_per_second: float,
    target_ms: float | None = None,
) -> LatencyStats:
    rows = list(values)
    if not rows:
        return LatencyStats(
            samples=0,
            min_ms=0.0,
            mean_ms=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            throughput_per_second=round(throughput_per_second, 3),
            target_ms=target_ms,
            target_hit_rate=None,
        )
    hit_rate = None
    if target_ms is not None:
        hit_rate = sum(1 for value in rows if value <= target_ms) / len(rows)
    return LatencyStats(
        samples=len(rows),
        min_ms=round(min(rows), 3),
        mean_ms=round(sum(rows) / len(rows), 3),
        p50_ms=round(_percentile(rows, 0.50), 3),
        p95_ms=round(_percentile(rows, 0.95), 3),
        p99_ms=round(_percentile(rows, 0.99), 3),
        max_ms=round(max(rows), 3),
        throughput_per_second=round(throughput_per_second, 3),
        target_ms=target_ms,
        target_hit_rate=None if hit_rate is None else round(hit_rate, 6),
    )


class PerformanceBenchmarker:
    def __init__(self, fast_path, mapreduce_engine):
        self.fast_path = fast_path
        self.mapreduce_engine = mapreduce_engine

    async def benchmark_realtime(self, request: RealtimeLoadBenchmarkRequest) -> RealtimeLoadBenchmarkResponse:
        profile = self.fast_path.get_profile(request.profile_id)
        warmup_input = request.inputs[0]
        for _ in range(request.warmup_runs):
            await self.fast_path.execute(FastDecisionRequest(profile_id=request.profile_id, input=warmup_input))

        queue: asyncio.Queue[tuple[float, str]] = asyncio.Queue()
        total_calls = len(request.inputs) * request.repeat_runs
        latencies: list[float] = []
        errors = 0

        wall_started = time.perf_counter()
        for _ in range(request.repeat_runs):
            for text in request.inputs:
                queue.put_nowait((time.perf_counter(), text))

        async def worker() -> None:
            nonlocal errors
            while True:
                try:
                    enqueued_at, text = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await self.fast_path.execute(
                        FastDecisionRequest(profile_id=request.profile_id, input=text)
                    )
                    elapsed_ms = (time.perf_counter() - enqueued_at) * 1000.0
                    latencies.append(elapsed_ms)
                    if not result.ok:
                        errors += 1
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(min(request.concurrency, total_calls))]
        await asyncio.gather(*workers)
        wall_seconds = max(time.perf_counter() - wall_started, 1e-9)
        throughput = total_calls / wall_seconds
        return RealtimeLoadBenchmarkResponse(
            profile_id=request.profile_id,
            calls=total_calls,
            concurrency=request.concurrency,
            errors=errors,
            wall_seconds=round(wall_seconds, 6),
            latency=_stats(latencies, throughput, profile.target_ms),
        )

    async def benchmark_mapreduce(self, request: MapReduceLoadBenchmarkRequest) -> MapReduceLoadBenchmarkResponse:
        for _ in range(request.warmup_runs):
            await self.mapreduce_engine.run(request.request)

        latencies: list[float] = []
        metrics: list[MapReduceRunMetric] = []
        total_failed = 0
        items_per_run = len(request.request.items)
        wall_started = time.perf_counter()

        for run_index in range(request.repeat_runs):
            started = time.perf_counter()
            response = await self.mapreduce_engine.run(request.request)
            elapsed = max(time.perf_counter() - started, 1e-9)
            elapsed_ms = elapsed * 1000.0
            latencies.append(elapsed_ms)
            total_failed += response.failed
            metrics.append(
                MapReduceRunMetric(
                    run=run_index + 1,
                    latency_ms=round(elapsed_ms, 3),
                    items=response.items,
                    ok=response.ok,
                    failed=response.failed,
                    items_per_second=round(response.items / elapsed, 3),
                )
            )

        wall_seconds = max(time.perf_counter() - wall_started, 1e-9)
        total_items = items_per_run * request.repeat_runs
        throughput = total_items / wall_seconds
        return MapReduceLoadBenchmarkResponse(
            runs=request.repeat_runs,
            items_per_run=items_per_run,
            total_items=total_items,
            total_failed=total_failed,
            wall_seconds=round(wall_seconds, 6),
            latency=_stats(latencies, throughput),
            run_metrics=metrics,
        )
