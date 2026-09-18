import pytest
from app.engine import DecisionEngine
from app.models import DecisionRequest


@pytest.mark.asyncio
async def test_rules_select_equipment_problem():
    engine = DecisionEngine()
    request = DecisionRequest(
        input="There is no hot water and the shower is not working.",
        provider="rules",
        decisions=[
            {
                "id": "intent",
                "question": "intent?",
                "choices": ["question", "equipment_problem", "other"],
                "min_confidence": 0.5,
                "keywords": {"equipment_problem": ["no hot water", "not working"]},
            }
        ],
    )
    response = await engine.decide(request)
    result = response.results[0]
    assert result.selected == "equipment_problem"
    assert result.confidence > 0.5
    assert result.requires_review is False


@pytest.mark.asyncio
async def test_abstains_when_no_rule_match():
    engine = DecisionEngine()
    request = DecisionRequest(
        input="Hello there",
        provider="rules",
        decisions=[
            {
                "id": "intent",
                "question": "intent?",
                "choices": ["a", "b", "c"],
                "min_confidence": 0.8,
            }
        ],
    )
    response = await engine.decide(request)
    result = response.results[0]
    assert result.selected is None
    assert result.abstained is True
    assert result.requires_review is True
