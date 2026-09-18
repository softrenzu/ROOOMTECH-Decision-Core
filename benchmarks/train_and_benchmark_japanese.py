from __future__ import annotations

import json
from pathlib import Path

from app.benchmark import BenchmarkRunner
from app.ml.local_classifier import LocalClassifierProvider
from app.models import BenchmarkRequest, TrainModelRequest


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "benchmarks" / "japanese_hospitality_intent_360.json"


def main():
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    train_examples = [
        {"text": row["text"], "label": row["label"]}
        for row in dataset["examples"]
        if row["split"] == "train"
    ]
    test_examples = [
        {"text": row["text"], "label": row["label"]}
        for row in dataset["examples"]
        if row["split"] == "test"
    ]

    local = LocalClassifierProvider()
    train_result = local.train(
        TrainModelRequest(
            decision_id="hospitality_intent_ja",
            model_name="ROOOMTECH Japanese Hospitality Intent Benchmark Model",
            examples=train_examples,
            feature_dim=8192,
            ngram_min=1,
            ngram_max=4,
            epochs=60,
            learning_rate=0.02,
            batch_size=128,
            validation_split=0.15,
            device="auto",
        )
    )

    benchmark = BenchmarkRunner(local).run_local(
        BenchmarkRequest(
            model_id=train_result.model_id,
            examples=test_examples,
            device="auto",
            warmup_runs=3,
            repeat_runs=10,
            batch_size=32,
            confidence_threshold=0.75,
            max_errors=25,
        )
    )

    output = {
        "dataset": {
            "name": dataset["name"],
            "version": dataset["version"],
            "total_examples": len(dataset["examples"]),
            "train_examples": len(train_examples),
            "held_out_test_examples": len(test_examples),
            "origin": dataset["origin"],
        },
        "training": train_result.model_dump(),
        "benchmark": benchmark.model_dump(),
        "comparison_notice": (
            "This benchmark measures ROOOMTECH Decision Core only. "
            "Do not present third-party speed or accuracy comparisons unless measured "
            "under authorized, controlled, reproducible conditions."
        ),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
