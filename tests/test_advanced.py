import pytest

from app.advanced_models import MapReduceRequest, RealtimeEvent, SchemaExtractionRequest
from app.engine import DecisionEngine
from app.mapreduce import MapReduceEngine
from app.operations import OperationalDecisionEngine
from app.realtime import RealtimeDispatcher
from app.schema_extraction import SchemaExtractor


@pytest.mark.asyncio
async def test_schema_extraction_from_key_value_text():
    extractor = SchemaExtractor()
    response = await extractor.extract(SchemaExtractionRequest(
        input="氏名: 山田太郎\n年齢: 42\n法人: はい",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "x-rtdc-aliases": ["氏名"]},
                "age": {"type": "integer", "x-rtdc-aliases": ["年齢"]},
                "company": {"type": "boolean", "x-rtdc-aliases": ["法人"]}
            },
            "required": ["name", "age", "company"],
            "additionalProperties": False
        },
        provider="heuristic",
    ))
    assert response.valid is True
    assert response.data == {"name": "山田太郎", "age": 42, "company": True}


@pytest.mark.asyncio
async def test_mapreduce_extract_and_count_by():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    extractor = SchemaExtractor(decision.model)
    engine = MapReduceEngine(decision, operations, extractor)
    request = MapReduceRequest(
        items=["category: refund", "category: refund", "category: question"],
        map={
            "kind": "extract",
            "config": {
                "schema": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}},
                    "required": ["category"]
                },
                "provider": "heuristic"
            }
        },
        reduce={"kind": "count_by", "path": "data.category"},
    )
    response = await engine.run(request)
    assert response.failed == 0
    assert response.reduced == {"refund": 2, "question": 1}


@pytest.mark.asyncio
async def test_realtime_ping():
    decision = DecisionEngine()
    operations = OperationalDecisionEngine(decision)
    dispatcher = RealtimeDispatcher(decision, operations, SchemaExtractor(decision.model))
    result = await dispatcher.dispatch(RealtimeEvent(kind="ping"))
    assert result.ok is True
    assert result.data == {"pong": True}
