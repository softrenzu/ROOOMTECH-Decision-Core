import pytest

from app.engine import DecisionEngine
from app.operation_models import (
    DetectionRequest,
    RankCandidate,
    RankRequest,
    RouteOption,
    RouteRequest,
    ScoreBand,
    ScoreRequest,
    VerificationCheck,
    VerificationRequest,
)
from app.operations import OperationalDecisionEngine


@pytest.mark.asyncio
async def test_detection_rules_returns_probability():
    ops = OperationalDecisionEngine(DecisionEngine())
    result = await ops.detect(
        DetectionRequest(
            input="返金をお願いします",
            property="refund request",
            provider="rules",
            threshold=0.8,
            keywords=["返金"],
        )
    )
    assert result.detected is True
    assert result.probability > 0.8


@pytest.mark.asyncio
async def test_route_rules_selects_keyword_route():
    ops = OperationalDecisionEngine(DecisionEngine())
    result = await ops.route(
        RouteRequest(
            input="ログインできません。パスワードを再設定したいです",
            provider="rules",
            min_confidence=0.5,
            routes=[
                RouteOption(id="billing", keywords=["請求"]),
                RouteOption(id="account", keywords=["ログイン", "パスワード"]),
            ],
        )
    )
    assert result.route == "account"


@pytest.mark.asyncio
async def test_scoring_returns_expected_numeric_value():
    ops = OperationalDecisionEngine(DecisionEngine())
    result = await ops.score(
        ScoreRequest(
            input="今すぐ対応してください。非常に緊急です",
            criterion="urgency",
            provider="rules",
            min_confidence=0.5,
            bands=[
                ScoreBand(id="low", value=1, keywords=["急ぎではない"]),
                ScoreBand(id="high", value=5, keywords=["今すぐ", "緊急"]),
            ],
        )
    )
    assert result.selected_band == "high"
    assert result.expected_score > 4.0


@pytest.mark.asyncio
async def test_verification_rules_flags_violation():
    ops = OperationalDecisionEngine(DecisionEngine())
    result = await ops.verify(
        VerificationRequest(
            artifact="管理者パスワードを公開します: abc123",
            provider="rules",
            checks=[
                VerificationCheck(
                    id="secret_exposure",
                    criterion="Must not expose passwords",
                    fail_threshold=0.8,
                    keywords=["パスワード"],
                )
            ],
        )
    )
    assert result.passed is False
    assert result.findings[0].status == "fail"


@pytest.mark.asyncio
async def test_local_ngram_rank_prefers_related_candidate():
    ops = OperationalDecisionEngine(DecisionEngine())
    result = await ops.rank(
        RankRequest(
            query="返金をお願いしたい",
            candidates=[
                RankCandidate(id="refund", text="商品代金の返金を希望しています"),
                RankCandidate(id="weather", text="明日の東京の天気を教えてください"),
            ],
            top_k=2,
        )
    )
    assert result.results[0].id == "refund"
    assert result.results[0].score > result.results[1].score
