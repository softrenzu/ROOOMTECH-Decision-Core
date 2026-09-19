import pytest

from app.dynamic_candidates import DynamicCandidateEngine, DynamicCandidateRequest
from app.engine import DecisionEngine
from app.operations import OperationalDecisionEngine


@pytest.mark.asyncio
async def test_local_dynamic_candidate_selects_best_match():
    engine = DynamicCandidateEngine(OperationalDecisionEngine(DecisionEngine()))
    request = DynamicCandidateRequest(
        query="新宿駅の近くでチェックイン方法を知りたい",
        candidates=[
            {"id": "billing", "text": "請求書 支払い クレジットカード"},
            {"id": "checkin", "text": "新宿駅 宿泊施設 チェックイン 入室 方法"},
            {"id": "wifi", "text": "WiFi パスワード インターネット 接続"},
        ],
        shortlist_k=3,
        final_k=2,
        min_selection_score=0.01,
        min_selection_margin=0.0,
    )
    result = await engine.select(request)
    assert result.selected_id == "checkin"
    assert result.results[0].id == "checkin"
    assert result.candidates_received == 3
    assert result.candidates_reranked == 0


@pytest.mark.asyncio
async def test_metadata_filter_limits_eligible_candidates():
    engine = DynamicCandidateEngine(OperationalDecisionEngine(DecisionEngine()))
    request = DynamicCandidateRequest(
        query="返金",
        metadata_equals={"region": "jp"},
        candidates=[
            {"id": "jp", "text": "返金 キャンセル", "metadata": {"region": "jp"}},
            {"id": "us", "text": "refund cancellation", "metadata": {"region": "us"}},
        ],
        shortlist_k=2,
        final_k=1,
        min_selection_margin=0.0,
    )
    result = await engine.select(request)
    assert result.candidates_eligible == 1
    assert result.selected_id == "jp"


@pytest.mark.asyncio
async def test_ambiguous_selection_routes_to_review():
    engine = DynamicCandidateEngine(OperationalDecisionEngine(DecisionEngine()))
    request = DynamicCandidateRequest(
        query="alpha beta",
        candidates=[
            {"id": "a", "text": "alpha beta one"},
            {"id": "b", "text": "alpha beta two"},
        ],
        shortlist_k=2,
        final_k=2,
        min_selection_score=0.0,
        min_selection_margin=0.5,
    )
    result = await engine.select(request)
    assert result.requires_review is True
    assert result.score_margin < 0.5


def test_candidate_request_rejects_excess_text_budget():
    with pytest.raises(ValueError):
        DynamicCandidateRequest(
            query="x",
            candidates=[{"id": "a", "text": "x" * 2000}],
            max_total_candidate_chars=1000,
        )


def test_candidate_and_graph_routes_are_wired():
    from app.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/v1/project/candidates/select" in paths
    assert "/v1/project/graphs/validate" in paths
