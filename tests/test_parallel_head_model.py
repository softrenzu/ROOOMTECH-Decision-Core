from pathlib import Path

import pytest
from pydantic import ValidationError

from app.parallel_head_model import (
    ParallelHeadModelProvider,
    ParallelHeadPredictRequest,
    TrainParallelHeadRequest,
)


def _training_request():
    examples = []
    for i in range(24):
        urgent = i % 2 == 0
        premium = i % 3 == 0
        examples.append(
            {
                "text": (
                    ("urgent refund " if urgent else "routine question ")
                    + ("premium customer" if premium else "basic customer")
                    + f" case{i}"
                ),
                "targets": {
                    "urgent": "yes" if urgent else "no",
                    "value": "high" if premium else "low",
                },
            }
        )
    return TrainParallelHeadRequest(
        model_name="support multihead",
        heads=[
            {"id": "urgent", "kind": "boolean", "labels": ["yes", "no"]},
            {
                "id": "value",
                "kind": "scalar",
                "labels": ["low", "high"],
                "values": {"low": 10.0, "high": 100.0},
            },
        ],
        examples=examples,
        feature_dim=256,
        hidden_dim=32,
        epochs=2,
        batch_size=8,
        validation_split=0.25,
    )


def test_parallel_head_training_schema_rejects_unknown_target():
    payload = _training_request().model_dump()
    payload["examples"][0]["targets"]["unknown"] = "x"
    with pytest.raises(ValidationError):
        TrainParallelHeadRequest.model_validate(payload)


def test_parallel_model_routes_are_wired():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/project/parallel-models/train" in paths
    assert "/v1/project/parallel-models/{model_id}/predict" in paths


def test_shared_encoder_trains_and_predicts_all_heads_in_one_forward(tmp_path):
    pytest.importorskip("torch")
    provider = ParallelHeadModelProvider(tmp_path / "models")
    summary = provider.train("project_a", _training_request())
    assert summary.project_id == "project_a"
    assert summary.raw_text_persisted is False
    assert len(summary.heads) == 2

    prediction = provider.predict(
        "project_a",
        summary.model_id,
        ParallelHeadPredictRequest(text="urgent refund premium customer"),
    )
    assert prediction.input_encoded_once is True
    assert prediction.output_generated_as_text is False
    assert {item.id for item in prediction.predictions} == {"urgent", "value"}
    for item in prediction.predictions:
        assert abs(sum(item.probabilities.values()) - 1.0) < 1e-5

    with pytest.raises(FileNotFoundError):
        provider.get_model("project_b", summary.model_id)
