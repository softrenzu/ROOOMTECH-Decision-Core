import asyncio

import pytest

from app.ml.adaptive_classifier import AdaptiveLocalClassifierProvider
from app.models import LocalPrediction


@pytest.mark.asyncio
async def test_cpu_path_skips_microbatch_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_LOCAL_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("RTDC_CPU_INFERENCE_SLOTS", "2")
    provider = AdaptiveLocalClassifierProvider()
    monkeypatch.setattr(provider, "resolve_device", lambda requested=None: "cpu")

    calls = []

    def fake_predict_many(model_id, texts, requested_device=None):
        rows = list(texts)
        calls.append(rows)
        return "cpu", [
            LocalPrediction(selected="account", confidence=0.9, scores={"account": 0.9, "billing": 0.1})
            for _ in rows
        ]

    monkeypatch.setattr(provider, "predict_many", fake_predict_many)
    results = await asyncio.gather(
        *(
            provider.evaluate_one_async("mdl_test", f"ログイン {index}", ["account", "billing"])
            for index in range(6)
        )
    )

    assert len(calls) == 6
    assert all(len(call) == 1 for call in calls)
    assert all(result["scores"]["account"] > result["scores"]["billing"] for result in results)
    info = provider.runtime_info()["inference_batching"]
    assert info["strategy"] == "cuda_microbatch_cpu_direct"
    assert info["cpu_direct_calls"] == 6


@pytest.mark.asyncio
async def test_cuda_path_keeps_microbatching(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_LOCAL_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("RTDC_LOCAL_BATCH_MAX", "16")
    monkeypatch.setenv("RTDC_LOCAL_BATCH_WAIT_MS", "2")
    provider = AdaptiveLocalClassifierProvider()
    monkeypatch.setattr(provider, "resolve_device", lambda requested=None: "cuda")

    calls = []

    def fake_predict_many(model_id, texts, requested_device=None):
        rows = list(texts)
        calls.append(rows)
        return "cuda", [
            LocalPrediction(selected="account", confidence=0.9, scores={"account": 0.9, "billing": 0.1})
            for _ in rows
        ]

    monkeypatch.setattr(provider, "predict_many", fake_predict_many)
    await asyncio.gather(
        *(
            provider.evaluate_one_async("mdl_test", f"ログイン {index}", ["account", "billing"])
            for index in range(8)
        )
    )

    assert len(calls) == 1
    assert len(calls[0]) == 8
