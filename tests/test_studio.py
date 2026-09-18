from __future__ import annotations

import tempfile

from app.calibration import CalibrationEngine
from app.review_store import ReviewStore
from app.studio_models import CalibrationRequest, ReviewCreate, ReviewResolve


def test_calibration_recommends_high_confidence_threshold():
    samples = []
    for confidence in [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.91, 0.90,
                       0.89, 0.88, 0.87, 0.86, 0.85, 0.84, 0.83, 0.82, 0.81, 0.80]:
        samples.append({"confidence": confidence, "correct": True})
    for confidence in [0.79, 0.75, 0.70, 0.65, 0.60]:
        samples.append({"confidence": confidence, "correct": False})

    result = CalibrationEngine.evaluate(
        CalibrationRequest(
            samples=samples,
            target_max_error_rate=0.01,
            min_accepted_samples=10,
            bins=10,
        )
    )
    assert result.recommended_threshold is not None
    assert result.accuracy_at_threshold == 1.0
    assert result.accepted_samples >= 10
    assert result.coverage_at_threshold > 0
    assert result.expected_calibration_error >= 0


def test_review_store_hashes_input_by_default_and_exports_only_retained_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = ReviewStore(f"{tmp}/reviews.sqlite3")
        hidden = store.create(
            ReviewCreate(
                decision_id="route",
                input_text="secret customer text",
                suggested_label="account",
                confidence=0.55,
                store_input=False,
            )
        )
        assert hidden.input_text is None
        assert hidden.input_sha256 is not None

        retained = store.create(
            ReviewCreate(
                decision_id="route",
                input_text="billing question",
                suggested_label="billing",
                confidence=0.60,
                store_input=True,
            )
        )
        resolved = store.resolve(
            retained.id,
            ReviewResolve(status="resolved", resolved_label="billing", reviewer="tester"),
        )
        assert resolved.status == "resolved"
        assert resolved.resolved_label == "billing"

        exported = store.export_training_examples()
        assert exported == [{"text": "billing question", "label": "billing"}]
        store.close()
