import pytest

from app.decision_accelerator import DecisionAccelerator, DecisionAcceleratorRequest
from app.engine import DecisionEngine


@pytest.mark.asyncio
async def test_typed_judgments_run_without_free_form_generation():
    engine = DecisionEngine()
    accelerator = DecisionAccelerator(engine)
    result = await accelerator.evaluate(
        DecisionAcceleratorRequest(
            state="urgent refund requested for premium customer",
            routing_mode="local_only",
            judgments=[
                {
                    "id": "needs_refund",
                    "kind": "boolean",
                    "instruction": "Is a refund requested?",
                    "true_keywords": ["refund"],
                    "false_keywords": ["no refund"],
                    "min_confidence": 0.0,
                    "min_margin": 0.0,
                    "max_entropy": 1.0,
                },
                {
                    "id": "priority",
                    "kind": "categorical",
                    "instruction": "Choose the support priority",
                    "options": [
                        {"id": "high", "keywords": ["urgent"]},
                        {"id": "normal", "keywords": ["routine"]},
                    ],
                    "min_confidence": 0.0,
                    "min_margin": 0.0,
                    "max_entropy": 1.0,
                },
                {
                    "id": "value_band",
                    "kind": "scalar",
                    "instruction": "Estimate customer value band",
                    "levels": [
                        {"id": "low", "value": 10.0, "keywords": ["basic"]},
                        {"id": "high", "value": 100.0, "keywords": ["premium"]},
                    ],
                    "min_confidence": 0.0,
                    "min_margin": 0.0,
                    "max_entropy": 1.0,
                },
            ],
        )
    )
    by_id = {item.id: item for item in result.results}
    assert by_id["needs_refund"].selected is True
    assert by_id["priority"].selected == "high"
    assert by_id["value_band"].selected == "high"
    assert by_id["value_band"].expected_value is not None
    assert by_id["value_band"].expected_value > 50
    assert result.routing.judgment_count == 3
    assert result.routing.external_call_count == 0
    assert result.routing.execution_shape == "parallel_local_batched_external"


class _FakeExternalModel:
    configured = True

    def __init__(self):
        self.calls = []

    async def evaluate(self, text, decisions):
        self.calls.append((text, [item.id for item in decisions]))
        return {
            item.id: {
                "scores": {choice: (0.99 if index == 0 else 0.01 / max(1, len(item.choices) - 1))
                           for index, choice in enumerate(item.choices)},
                "reason_codes": ["FAKE_EXTERNAL_FOR_TEST"],
            }
            for item in decisions
        }


@pytest.mark.asyncio
async def test_auto_mode_batches_only_unresolved_judgments_into_one_external_call():
    engine = DecisionEngine()
    fake = _FakeExternalModel()
    engine.model = fake
    accelerator = DecisionAccelerator(engine)
    result = await accelerator.evaluate(
        DecisionAcceleratorRequest(
            state="known signal",
            routing_mode="auto",
            max_external_judgments=1,
            judgments=[
                {
                    "id": "resolved",
                    "kind": "boolean",
                    "instruction": "Known signal?",
                    "true_keywords": ["known"],
                    "min_confidence": 0.6,
                    "min_margin": 0.1,
                    "max_entropy": 0.9,
                },
                {
                    "id": "uncertain",
                    "kind": "categorical",
                    "instruction": "Unknown route?",
                    "options": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    "min_confidence": 0.8,
                    "min_margin": 0.2,
                    "max_entropy": 0.5,
                },
            ],
        )
    )
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == ["uncertain"]
    assert result.routing.locally_resolved == 1
    assert result.routing.unresolved_before_external == 1
    assert result.routing.external_judgments == 1
    assert result.routing.external_call_count == 1
    by_id = {item.id: item for item in result.results}
    assert by_id["resolved"].external_escalated is False
    assert by_id["uncertain"].external_escalated is True


def test_decision_accelerator_route_is_wired():
    from app.main import app

    assert "/v1/project/accelerate" in set(app.openapi()["paths"])
