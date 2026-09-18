from __future__ import annotations

import math
import time

from app.models import (
    BenchmarkError,
    BenchmarkLabelMetrics,
    BenchmarkRequest,
    BenchmarkResponse,
    BenchmarkTiming,
    LocalPrediction,
)
from app.ml.local_classifier import LocalClassifierProvider


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    fraction = pos - lo
    return ordered[lo] * (1.0 - fraction) + ordered[hi] * fraction


def classification_metrics(
    expected: list[str],
    predictions: list[LocalPrediction],
    confidence_threshold: float,
) -> dict:
    if len(expected) != len(predictions):
        raise ValueError("expected and predictions must have the same length")
    if not expected:
        raise ValueError("benchmark requires at least one example")

    labels = list(dict.fromkeys(expected + [p.selected for p in predictions]))
    confusion = {label: {other: 0 for other in labels} for label in labels}
    correct = 0
    errors: list[BenchmarkError] = []

    for index, (truth, pred) in enumerate(zip(expected, predictions)):
        confusion[truth][pred.selected] += 1
        if truth == pred.selected:
            correct += 1
        else:
            errors.append(
                BenchmarkError(
                    index=index,
                    expected=truth,
                    predicted=pred.selected,
                    confidence=pred.confidence,
                )
            )

    per_label: list[BenchmarkLabelMetrics] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in labels if other != label)
        fn = sum(confusion[label][other] for other in labels if other != label)
        support = sum(confusion[label].values())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
        precisions.append(precision)
        recalls.append(recall)
        f1s.append(f1)
        per_label.append(
            BenchmarkLabelMetrics(
                label=label,
                precision=round(precision, 6),
                recall=round(recall, 6),
                f1=round(f1, 6),
                support=support,
            )
        )

    confidence_sum = sum(float(p.confidence) for p in predictions)
    covered = [
        (truth, pred)
        for truth, pred in zip(expected, predictions)
        if pred.confidence >= confidence_threshold
    ]
    selective_accuracy = (
        sum(1 for truth, pred in covered if truth == pred.selected) / len(covered)
        if covered
        else None
    )

    ece = 0.0
    n = len(predictions)
    for bin_index in range(10):
        low = bin_index / 10
        high = (bin_index + 1) / 10
        members = [
            (truth, pred)
            for truth, pred in zip(expected, predictions)
            if (low <= pred.confidence < high) or (bin_index == 9 and pred.confidence == 1.0)
        ]
        if not members:
            continue
        bin_accuracy = sum(1 for truth, pred in members if truth == pred.selected) / len(members)
        bin_confidence = sum(pred.confidence for _, pred in members) / len(members)
        ece += (len(members) / n) * abs(bin_accuracy - bin_confidence)

    return {
        "accuracy": round(correct / len(expected), 6),
        "macro_precision": round(sum(precisions) / len(precisions), 6),
        "macro_recall": round(sum(recalls) / len(recalls), 6),
        "macro_f1": round(sum(f1s) / len(f1s), 6),
        "mean_confidence": round(confidence_sum / len(predictions), 6),
        "expected_calibration_error": round(ece, 6),
        "coverage_at_threshold": round(len(covered) / len(predictions), 6),
        "selective_accuracy_at_threshold": (
            None if selective_accuracy is None else round(selective_accuracy, 6)
        ),
        "per_label": per_label,
        "confusion_matrix": confusion,
        "errors": errors,
    }


class BenchmarkRunner:
    def __init__(self, local: LocalClassifierProvider):
        self.local = local

    def run_local(self, request: BenchmarkRequest) -> BenchmarkResponse:
        texts = [item.text for item in request.examples]
        expected = [item.label for item in request.examples]

        cold_started = time.perf_counter()
        device, _ = self.local.predict_many(request.model_id, [texts[0]], request.device)
        cold_start_ms = (time.perf_counter() - cold_started) * 1000.0

        warmup_inputs = texts[: min(len(texts), request.batch_size)]
        for _ in range(request.warmup_runs):
            self.local.predict_many(request.model_id, warmup_inputs, request.device)

        latency_per_item_ms: list[float] = []
        last_predictions: list[LocalPrediction] = []
        timed_seconds = 0.0
        timed_items = 0

        for _ in range(request.repeat_runs):
            current_predictions: list[LocalPrediction] = []
            for start in range(0, len(texts), request.batch_size):
                batch = texts[start : start + request.batch_size]
                started = time.perf_counter()
                device, batch_predictions = self.local.predict_many(
                    request.model_id, batch, request.device
                )
                elapsed = time.perf_counter() - started
                timed_seconds += elapsed
                timed_items += len(batch)
                latency_per_item_ms.extend(
                    [(elapsed * 1000.0) / len(batch)] * len(batch)
                )
                current_predictions.extend(batch_predictions)
            last_predictions = current_predictions

        metric_data = classification_metrics(
            expected,
            last_predictions,
            request.confidence_threshold,
        )

        mean_item_ms = (
            sum(latency_per_item_ms) / len(latency_per_item_ms)
            if latency_per_item_ms
            else 0.0
        )
        throughput = timed_items / timed_seconds if timed_seconds > 0 else 0.0
        timings = BenchmarkTiming(
            cold_start_ms=round(cold_start_ms, 3),
            mean_item_ms=round(mean_item_ms, 3),
            p50_item_ms=round(_percentile(latency_per_item_ms, 0.50), 3),
            p95_item_ms=round(_percentile(latency_per_item_ms, 0.95), 3),
            p99_item_ms=round(_percentile(latency_per_item_ms, 0.99), 3),
            throughput_items_per_second=round(throughput, 3),
            timed_seconds=round(timed_seconds, 6),
        )

        return BenchmarkResponse(
            model_id=request.model_id,
            device=device,
            examples=len(request.examples),
            repeat_runs=request.repeat_runs,
            batch_size=request.batch_size,
            confidence_threshold=request.confidence_threshold,
            timings=timings,
            accuracy=metric_data["accuracy"],
            macro_precision=metric_data["macro_precision"],
            macro_recall=metric_data["macro_recall"],
            macro_f1=metric_data["macro_f1"],
            mean_confidence=metric_data["mean_confidence"],
            expected_calibration_error=metric_data["expected_calibration_error"],
            coverage_at_threshold=metric_data["coverage_at_threshold"],
            selective_accuracy_at_threshold=metric_data["selective_accuracy_at_threshold"],
            per_label=metric_data["per_label"],
            confusion_matrix=metric_data["confusion_matrix"],
            errors=metric_data["errors"][: request.max_errors],
        )
