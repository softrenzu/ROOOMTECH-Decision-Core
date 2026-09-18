from app.benchmark import _percentile, classification_metrics
from app.models import LocalPrediction
from benchmarks.japanese_hospitality_intent_360 import build_dataset


def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(values, 0.5) == 2.5


def test_classification_metrics_are_correct():
    expected = ["a", "a", "b", "b"]
    predictions = [
        LocalPrediction(selected="a", confidence=0.9, scores={"a": 0.9, "b": 0.1}),
        LocalPrediction(selected="b", confidence=0.6, scores={"a": 0.4, "b": 0.6}),
        LocalPrediction(selected="b", confidence=0.8, scores={"a": 0.2, "b": 0.8}),
        LocalPrediction(selected="b", confidence=0.7, scores={"a": 0.3, "b": 0.7}),
    ]
    metrics = classification_metrics(expected, predictions, confidence_threshold=0.75)
    assert metrics["accuracy"] == 0.75
    assert metrics["coverage_at_threshold"] == 0.5
    assert metrics["selective_accuracy_at_threshold"] == 1.0
    assert len(metrics["errors"]) == 1
    assert metrics["confusion_matrix"]["a"]["b"] == 1


def test_japanese_benchmark_dataset_is_balanced_and_held_out():
    dataset = build_dataset()
    assert len(dataset["examples"]) == 360
    labels = dataset["labels"]
    assert len(labels) == 6
    for label in labels:
        rows = [row for row in dataset["examples"] if row["label"] == label]
        assert len(rows) == 60
        assert sum(row["split"] == "train" for row in rows) == 42
        assert sum(row["split"] == "test" for row in rows) == 18
