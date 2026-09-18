from __future__ import annotations

from app.studio_models import (
    CalibrationRequest,
    CalibrationResponse,
    ReliabilityBin,
    ThresholdPoint,
)


class CalibrationEngine:
    """Empirical calibration analysis over operator-owned held-out outcomes.

    This module intentionally does not depend on, query, imitate, or benchmark any
    third-party decision service. It only consumes confidence/correctness pairs supplied
    by the operator.
    """

    @staticmethod
    def evaluate(request: CalibrationRequest) -> CalibrationResponse:
        rows = request.samples
        n = len(rows)
        correct_total = sum(1 for row in rows if row.correct)
        accuracy = correct_total / n
        mean_confidence = sum(row.confidence for row in rows) / n
        brier = sum((row.confidence - (1.0 if row.correct else 0.0)) ** 2 for row in rows) / n

        reliability: list[ReliabilityBin] = []
        ece = 0.0
        for idx in range(request.bins):
            lower = idx / request.bins
            upper = (idx + 1) / request.bins
            if idx == request.bins - 1:
                bucket = [row for row in rows if lower <= row.confidence <= upper]
            else:
                bucket = [row for row in rows if lower <= row.confidence < upper]
            if not bucket:
                reliability.append(ReliabilityBin(lower=lower, upper=upper, count=0))
                continue
            bucket_conf = sum(row.confidence for row in bucket) / len(bucket)
            bucket_acc = sum(1 for row in bucket if row.correct) / len(bucket)
            gap = abs(bucket_conf - bucket_acc)
            ece += (len(bucket) / n) * gap
            reliability.append(
                ReliabilityBin(
                    lower=round(lower, 6),
                    upper=round(upper, 6),
                    count=len(bucket),
                    mean_confidence=round(bucket_conf, 6),
                    accuracy=round(bucket_acc, 6),
                    gap=round(gap, 6),
                )
            )

        thresholds = sorted({0.0, 1.0, *(row.confidence for row in rows)})
        curve: list[ThresholdPoint] = []
        best: ThresholdPoint | None = None
        for threshold in thresholds:
            accepted_rows = [row for row in rows if row.confidence >= threshold]
            accepted = len(accepted_rows)
            if accepted:
                accepted_accuracy = sum(1 for row in accepted_rows if row.correct) / accepted
                error_rate = 1.0 - accepted_accuracy
            else:
                accepted_accuracy = None
                error_rate = None
            point = ThresholdPoint(
                threshold=round(threshold, 6),
                accepted=accepted,
                coverage=round(accepted / n, 6),
                accuracy=round(accepted_accuracy, 6) if accepted_accuracy is not None else None,
                error_rate=round(error_rate, 6) if error_rate is not None else None,
            )
            curve.append(point)
            if (
                accepted >= request.min_accepted_samples
                and error_rate is not None
                and error_rate <= request.target_max_error_rate
            ):
                if best is None or point.coverage > best.coverage or (
                    point.coverage == best.coverage and point.threshold < best.threshold
                ):
                    best = point

        if len(curve) > 250:
            step = max(1, len(curve) // 249)
            sampled = curve[::step]
            if sampled[-1] != curve[-1]:
                sampled.append(curve[-1])
            curve = sampled[:250]

        return CalibrationResponse(
            samples=n,
            accuracy=round(accuracy, 6),
            mean_confidence=round(mean_confidence, 6),
            expected_calibration_error=round(ece, 6),
            brier_score=round(brier, 6),
            target_max_error_rate=request.target_max_error_rate,
            recommended_threshold=best.threshold if best else None,
            accepted_samples=best.accepted if best else 0,
            coverage_at_threshold=best.coverage if best else 0.0,
            accuracy_at_threshold=best.accuracy if best else None,
            reliability=reliability,
            threshold_curve=curve,
        )
