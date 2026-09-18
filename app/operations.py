from __future__ import annotations

import time

from app.engine import DecisionEngine
from app.ml.local_classifier import hashed_char_features
from app.models import DecisionRequest, DecisionSpec
from app.operation_models import (
    DetectionRequest,
    DetectionResponse,
    FeatureExtractionRequest,
    FeatureExtractionResponse,
    RankRequest,
    RankedItem,
    RankResponse,
    RouteRequest,
    RouteResponse,
    ScoreRequest,
    ScoreResponse,
    VerificationFinding,
    VerificationRequest,
    VerificationResponse,
)


def _scores(result) -> dict[str, float]:
    return {item.choice: float(item.probability) for item in result.scores}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class OperationalDecisionEngine:
    """Higher-level decision primitives built on the independent Decision Core.

    These APIs are convenience wrappers around typed probabilistic decisions. They do
    not reproduce any third-party protocol, prompt, SDK or private service behavior.
    """

    def __init__(self, engine: DecisionEngine):
        self.engine = engine

    async def detect(self, request: DetectionRequest) -> DetectionResponse:
        spec = DecisionSpec(
            id="property_detection",
            question=f"Is this property present in the input? Property: {request.property}",
            choices=["present", "absent"],
            min_confidence=request.review_below,
            min_margin=0.0,
            max_entropy=1.0,
            allow_abstain=False,
            keywords={"present": request.keywords},
            model_id=request.model_id,
        )
        response = await self.engine.decide(
            DecisionRequest(input=request.input, decisions=[spec], provider=request.provider)
        )
        result = response.results[0]
        scores = _scores(result)
        probability = _clamp01(scores.get("present", 0.0))
        return DetectionResponse(
            detected=probability >= request.threshold,
            probability=round(probability, 6),
            threshold=request.threshold,
            requires_review=result.requires_review,
            result=result,
        )

    async def route(self, request: RouteRequest) -> RouteResponse:
        route_text = "; ".join(
            f"{item.id}: {item.description[:100]}" for item in request.routes
        )
        question = f"Choose the best route for this input. Routes: {route_text}"[:1000]
        spec = DecisionSpec(
            id="route",
            question=question,
            choices=[item.id for item in request.routes],
            min_confidence=request.min_confidence,
            min_margin=0.05,
            max_entropy=0.90,
            allow_abstain=True,
            keywords={item.id: item.keywords for item in request.routes if item.keywords},
            model_id=request.model_id,
        )
        response = await self.engine.decide(
            DecisionRequest(input=request.input, decisions=[spec], provider=request.provider)
        )
        result = response.results[0]
        return RouteResponse(
            route=result.selected,
            confidence=result.confidence,
            requires_review=result.requires_review,
            scores=_scores(result),
            result=result,
        )

    async def score(self, request: ScoreRequest) -> ScoreResponse:
        band_text = "; ".join(
            f"{item.id}={item.value}: {item.description[:80]}" for item in request.bands
        )
        question = f"Score the input for: {request.criterion}. Rubric: {band_text}"[:1000]
        spec = DecisionSpec(
            id="score",
            question=question,
            choices=[item.id for item in request.bands],
            min_confidence=request.min_confidence,
            min_margin=0.0,
            max_entropy=0.95,
            allow_abstain=True,
            keywords={item.id: item.keywords for item in request.bands if item.keywords},
            model_id=request.model_id,
        )
        response = await self.engine.decide(
            DecisionRequest(input=request.input, decisions=[spec], provider=request.provider)
        )
        result = response.results[0]
        probabilities = _scores(result)
        values = {item.id: item.value for item in request.bands}
        expected = sum(probabilities.get(key, 0.0) * value for key, value in values.items())
        return ScoreResponse(
            selected_band=result.selected,
            expected_score=round(expected, 6),
            confidence=result.confidence,
            requires_review=result.requires_review,
            band_probabilities=probabilities,
            result=result,
        )

    async def verify(self, request: VerificationRequest) -> VerificationResponse:
        started = time.perf_counter()
        specs: list[DecisionSpec] = []
        for check in request.checks:
            specs.append(
                DecisionSpec(
                    id=check.id,
                    question=f"Does the artifact violate this check? {check.criterion}"[:1000],
                    choices=["violation", "clear"],
                    min_confidence=check.review_below,
                    min_margin=0.0,
                    max_entropy=1.0,
                    allow_abstain=False,
                    keywords={"violation": check.keywords},
                    model_id=request.model_id,
                )
            )
        response = await self.engine.decide(
            DecisionRequest(input=request.artifact, decisions=specs, provider=request.provider)
        )
        checks = {item.id: item for item in request.checks}
        findings: list[VerificationFinding] = []
        any_review = False
        any_fail = False
        for result in response.results:
            check = checks[result.id]
            probability = _clamp01(_scores(result).get("violation", 0.0))
            if probability >= check.fail_threshold:
                status = "fail"
                any_fail = True
            elif result.requires_review:
                status = "review"
                any_review = True
            else:
                status = "pass"
            findings.append(
                VerificationFinding(
                    id=check.id,
                    severity=check.severity,
                    status=status,
                    violation_probability=round(probability, 6),
                    confidence=result.confidence,
                    evidence=result.evidence,
                )
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        return VerificationResponse(
            passed=not any_fail and not any_review,
            requires_review=any_review,
            findings=findings,
            latency_ms=round(elapsed, 3),
        )

    async def rank(self, request: RankRequest) -> RankResponse:
        started = time.perf_counter()
        if request.method == "local_ngram":
            query_vector = hashed_char_features(
                request.query, request.feature_dim, request.ngram_min, request.ngram_max
            )
            scored: list[tuple[str, float]] = []
            for candidate in request.candidates:
                vector = hashed_char_features(
                    candidate.text, request.feature_dim, request.ngram_min, request.ngram_max
                )
                similarity = sum(a * b for a, b in zip(query_vector, vector))
                scored.append((candidate.id, _clamp01(similarity)))
        else:
            if not self.engine.model.configured:
                raise RuntimeError("OpenAI-compatible provider is not configured")
            scored = []
            weights = {"none": 0.0, "low": 0.33, "medium": 0.66, "high": 1.0}
            for offset in range(0, len(request.candidates), 30):
                chunk = request.candidates[offset : offset + 30]
                specs = [
                    DecisionSpec(
                        id=f"candidate_{offset + index}",
                        question=(
                            "Rate how relevant this candidate is to the query. "
                            f"Candidate: {candidate.text[:700]}"
                        )[:1000],
                        choices=["none", "low", "medium", "high"],
                        min_confidence=0.0,
                        min_margin=0.0,
                        max_entropy=1.0,
                        allow_abstain=False,
                    )
                    for index, candidate in enumerate(chunk)
                ]
                response = await self.engine.decide(
                    DecisionRequest(
                        input=request.query,
                        decisions=specs,
                        provider="openai_compatible",
                    )
                )
                for candidate, result in zip(chunk, response.results):
                    probabilities = _scores(result)
                    score = sum(probabilities.get(label, 0.0) * weight for label, weight in weights.items())
                    scored.append((candidate.id, _clamp01(score)))

        scored.sort(key=lambda item: (-item[1], item[0]))
        top_k = min(request.top_k, len(scored))
        results = [
            RankedItem(id=item_id, score=round(score, 6), rank=index + 1)
            for index, (item_id, score) in enumerate(scored[:top_k])
        ]
        elapsed = (time.perf_counter() - started) * 1000.0
        return RankResponse(
            results=results,
            method=request.method,
            latency_ms=round(elapsed, 3),
            candidates_scored=len(scored),
        )

    async def extract_features(self, request: FeatureExtractionRequest) -> FeatureExtractionResponse:
        specs = [
            DecisionSpec(
                id=item.id,
                question=item.question,
                choices=item.choices,
                min_confidence=item.min_confidence,
                min_margin=0.0,
                max_entropy=0.95,
                allow_abstain=True,
                keywords=item.keywords,
                model_id=item.model_id,
            )
            for item in request.features
        ]
        response = await self.engine.decide(
            DecisionRequest(input=request.input, decisions=specs, provider=request.provider)
        )
        return FeatureExtractionResponse(
            selected={item.id: item.selected for item in response.results},
            probabilities={item.id: _scores(item) for item in response.results},
            requires_review={item.id: item.requires_review for item in response.results},
            results=response.results,
            latency_ms=response.latency_ms,
        )
