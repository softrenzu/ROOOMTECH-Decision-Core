import pytest

from app.decision_graph import (
    DecisionGraphRuntime,
    DecisionGraphStore,
    GraphDefinition,
    GraphRunRequest,
)
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine
from app.review_store import ReviewStore
from app.schema_extraction import SchemaExtractor


def runtime(review_store=None):
    engine = DecisionEngine()
    return DecisionGraphRuntime(
        engine,
        OperationalDecisionEngine(engine),
        SchemaExtractor(engine.model),
        reviews=review_store,
    )


def sample_graph():
    return GraphDefinition.model_validate(
        {
            "graph_id": "support_flow",
            "name": "Support decision flow",
            "nodes": [
                {
                    "id": "refund",
                    "kind": "detect",
                    "config": {
                        "property": "refund request",
                        "provider": "rules",
                        "threshold": 0.6,
                        "review_below": 0.0,
                        "keywords": ["返金", "refund"],
                    },
                },
                {
                    "id": "gate",
                    "kind": "gate",
                    "config": {
                        "conditions": [
                            {"path": "nodes.refund.detected", "op": "eq", "value": True}
                        ]
                    },
                },
                {
                    "id": "emit",
                    "kind": "action",
                    "config": {
                        "action": "emit",
                        "payload": {
                            "kind": "refund_review",
                            "probability": "$nodes.refund.probability",
                        },
                    },
                },
            ],
            "edges": [
                {"source": "refund", "target": "gate"},
                {
                    "source": "gate",
                    "target": "emit",
                    "condition": {"path": "nodes.gate.passed", "op": "eq", "value": True},
                },
            ],
        }
    )


def test_graph_rejects_cycles():
    with pytest.raises(ValueError, match="acyclic"):
        GraphDefinition.model_validate(
            {
                "graph_id": "bad",
                "name": "Bad graph",
                "nodes": [
                    {"id": "a", "kind": "gate", "config": {"conditions": [{"path": "input", "op": "truthy"}]}},
                    {"id": "b", "kind": "gate", "config": {"conditions": [{"path": "input", "op": "truthy"}]}},
                ],
                "edges": [
                    {"source": "a", "target": "b"},
                    {"source": "b", "target": "a"},
                ],
            }
        )


@pytest.mark.asyncio
async def test_graph_conditionally_executes_action_in_dry_run():
    result = await runtime().run(
        sample_graph(),
        1,
        GraphRunRequest(input="返金をお願いします", dry_run=True),
    )
    assert result.status == "completed"
    assert result.outputs["refund"]["detected"] is True
    assert result.outputs["gate"]["passed"] is True
    assert result.outputs["emit"]["status"] == "simulated"
    assert result.outputs["emit"]["executed"] is False
    assert result.actions[0]["payload"]["kind"] == "refund_review"


@pytest.mark.asyncio
async def test_graph_skips_downstream_when_edge_condition_is_false():
    result = await runtime().run(
        sample_graph(),
        1,
        GraphRunRequest(input="ログインできません", dry_run=True),
    )
    statuses = {item.node_id: item.status for item in result.trace}
    assert result.outputs["refund"]["detected"] is False
    assert result.outputs["gate"]["passed"] is False
    assert statuses["emit"] == "skipped"
    assert result.actions == []


@pytest.mark.asyncio
async def test_review_node_uses_privacy_default():
    reviews = ReviewStore(":memory:")
    definition = GraphDefinition.model_validate(
        {
            "graph_id": "review_flow",
            "name": "Review flow",
            "nodes": [
                {
                    "id": "detect",
                    "kind": "detect",
                    "config": {
                        "property": "urgent",
                        "provider": "rules",
                        "threshold": 0.5,
                        "review_below": 0.0,
                        "keywords": ["緊急"],
                    },
                },
                {
                    "id": "human",
                    "kind": "review",
                    "config": {
                        "decision_id": "urgent_review",
                        "suggested_label_path": "nodes.detect.detected",
                        "confidence_path": "nodes.detect.probability",
                        "store_input": False,
                        "include_paths": ["nodes.detect"],
                    },
                },
            ],
            "edges": [{"source": "detect", "target": "human"}],
        }
    )
    result = await runtime(reviews).run(
        definition,
        1,
        GraphRunRequest(input="緊急で対応してください", dry_run=True),
    )
    assert result.status == "review"
    assert len(result.review_ids) == 1
    item = reviews.get(result.review_ids[0])
    assert item.input_text is None
    assert item.input_sha256 is not None
    reviews.close()


def test_graph_store_versions_and_does_not_retain_input_by_default():
    store = DecisionGraphStore(":memory:")
    definition = sample_graph()
    first = store.put_graph("project-a", definition)
    second = store.put_graph("project-a", definition)
    assert first.version == 1
    assert second.version == 2
    loaded, summary = store.get_graph("project-a", "support_flow")
    assert summary.version == 2
    assert loaded.graph_id == "support_flow"
    with pytest.raises(FileNotFoundError):
        store.get_graph("project-b", "support_flow")
    store.close()
