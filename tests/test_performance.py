import pytest

from app.advanced_models import MapReduceRequest, RealtimeEvent
from app.engine import DecisionEngine
from app.fastpath import FastPathEngine
from app.mapreduce import MapReduceEngine
from app.operations import OperationalDecisionEngine
from app.performance import PerformanceBenchmarker
from app.performance_models import (
    FastDecisionRequest,
    FastProfileCreate,
    MapReduceLoadBenchmarkRequest,
    RealtimeLoadBenchmarkRequest,
)
from app.realtime import RealtimeDispatcher
from app.schema_extraction import SchemaExtractor


def stack():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    extractor = SchemaExtractor(decision.model)
    fast = FastPathEngine(decision, operations, extractor)
    mapreduce = MapReduceEngine(decision, operations, extractor)
    return decision, operations, extractor, fast, mapreduce


@pytest.mark.asyncio
async def test_fast_profile_prevalidates_rules_route():
    _, _, _, fast, _ = stack()
    profile = await fast.create_profile(
        FastProfileCreate(
            profile_id="support-route",
            kind="route",
            target_ms=1000,
            request={
                "provider": "rules",
                "min_confidence": 0.5,
                "routes": [
                    {"id": "billing", "keywords": ["請求"]},
                    {"id": "account", "keywords": ["ログイン", "パスワード"]},
                ],
            },
        )
    )
    assert profile.provider == "rules"
    result = await fast.execute(
        FastDecisionRequest(profile_id="support-route", input="ログインできません")
    )
    assert result.ok is True
    assert result.data["route"] == "account"
    assert result.target_ms == 1000


@pytest.mark.asyncio
async def test_fast_profile_rejects_network_provider():
    _, _, _, fast, _ = stack()
    with pytest.raises(ValueError):
        await fast.create_profile(
            FastProfileCreate(
                kind="route",
                request={
                    "provider": "auto",
                    "routes": [{"id": "a"}, {"id": "b"}],
                },
            )
        )


@pytest.mark.asyncio
async def test_realtime_dispatcher_accepts_fast_event():
    decision, operations, extractor, fast, _ = stack()
    await fast.create_profile(
        FastProfileCreate(
            profile_id="detect-refund",
            kind="detect",
            target_ms=1000,
            request={
                "property": "refund request",
                "provider": "rules",
                "threshold": 0.8,
                "keywords": ["返金"],
            },
        )
    )
    dispatcher = RealtimeDispatcher(decision, operations, extractor, fast_path=fast)
    result = await dispatcher.dispatch(
        RealtimeEvent(
            kind="fast",
            request={"profile_id": "detect-refund", "input": "返金をお願いします"},
        )
    )
    assert result.ok is True
    assert result.data["ok"] is True
    assert result.data["data"]["detected"] is True


@pytest.mark.asyncio
async def test_realtime_load_benchmark_reports_latency_and_throughput():
    _, _, _, fast, mapreduce = stack()
    await fast.create_profile(
        FastProfileCreate(
            profile_id="route-bench",
            kind="route",
            target_ms=1000,
            request={
                "provider": "rules",
                "min_confidence": 0.5,
                "routes": [
                    {"id": "billing", "keywords": ["請求"]},
                    {"id": "account", "keywords": ["ログイン"]},
                ],
            },
        )
    )
    benchmarker = PerformanceBenchmarker(fast, mapreduce)
    result = await benchmarker.benchmark_realtime(
        RealtimeLoadBenchmarkRequest(
            profile_id="route-bench",
            inputs=["ログインできません", "請求を確認したい"],
            repeat_runs=3,
            concurrency=2,
            warmup_runs=1,
        )
    )
    assert result.calls == 6
    assert result.errors == 0
    assert result.latency.samples == 6
    assert result.latency.throughput_per_second > 0
    assert result.latency.target_hit_rate is not None


@pytest.mark.asyncio
async def test_mapreduce_load_benchmark_executes_real_jobs():
    _, _, _, fast, mapreduce = stack()
    benchmarker = PerformanceBenchmarker(fast, mapreduce)
    request = MapReduceRequest(
        items=["返金をお願いします", "質問があります"],
        map={
            "kind": "detect",
            "config": {
                "property": "refund request",
                "provider": "rules",
                "threshold": 0.8,
                "keywords": ["返金"],
            },
        },
        reduce={"kind": "collect"},
        concurrency=2,
        include_map_results=False,
    )
    result = await benchmarker.benchmark_mapreduce(
        MapReduceLoadBenchmarkRequest(request=request, repeat_runs=2, warmup_runs=0)
    )
    assert result.runs == 2
    assert result.total_items == 4
    assert result.total_failed == 0
    assert len(result.run_metrics) == 2
    assert result.latency.throughput_per_second > 0
