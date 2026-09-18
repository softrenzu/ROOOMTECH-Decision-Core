import asyncio
import math

import pytest

from app.ml.local_classifier import LocalClassifierProvider, hashed_char_features, normalize_text
from app.models import LocalPrediction


def test_japanese_normalization_and_features():
    assert normalize_text(" Ｗｉ－Ｆｉが使えない ") == "wi-fiが使えない"
    vector = hashed_char_features("エアコンが動きません", feature_dim=512, ngram_min=1, ngram_max=3)
    assert len(vector) == 512
    norm = math.sqrt(sum(value * value for value in vector))
    assert abs(norm - 1.0) < 1e-6
    assert any(value != 0 for value in vector)


def test_hash_is_deterministic_and_language_agnostic():
    first = hashed_char_features("チェックインは何時ですか", feature_dim=256)
    second = hashed_char_features("チェックインは何時ですか", feature_dim=256)
    english = hashed_char_features("What time is check-in?", feature_dim=256)
    assert first == second
    assert first != english


@pytest.mark.asyncio
async def test_concurrent_inference_is_micro_batched(monkeypatch, tmp_path):
    monkeypatch.setenv("RTDC_LOCAL_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("RTDC_LOCAL_BATCH_MAX", "16")
    monkeypatch.setenv("RTDC_LOCAL_BATCH_WAIT_MS", "2")
    provider = LocalClassifierProvider()

    monkeypatch.setattr(provider, "resolve_device", lambda requested=None: "cpu")
    calls: list[list[str]] = []

    def fake_predict_many(model_id, texts, requested_device=None):
        rows = list(texts)
        calls.append(rows)
        predictions = [
            LocalPrediction(selected="account", confidence=0.9, scores={"account": 0.9, "billing": 0.1})
            for _ in rows
        ]
        return "cpu", predictions

    monkeypatch.setattr(provider, "predict_many", fake_predict_many)

    results = await asyncio.gather(
        *(
            provider.evaluate_one_async("mdl_test", f"ログインできません {index}", ["account", "billing"])
            for index in range(8)
        )
    )

    assert len(calls) == 1
    assert len(calls[0]) == 8
    assert all(result["scores"]["account"] > result["scores"]["billing"] for result in results)
    info = provider.runtime_info()["inference_batching"]
    assert info["batches_executed"] == 1
    assert info["items_executed"] == 8
