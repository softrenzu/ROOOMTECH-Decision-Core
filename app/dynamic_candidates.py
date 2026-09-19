from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.operation_models import RankCandidate, RankRequest
from app.operations import OperationalDecisionEngine
from app.semantic_matrix import sparse_cosine, sparse_signature


class DynamicCandidate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DynamicCandidateRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    candidates: list[DynamicCandidate] = Field(min_length=1, max_length=50_000)
    metadata_equals: dict[str, Any] = Field(default_factory=dict)
    shortlist_k: int = Field(default=100, ge=1, le=500)
    final_k: int = Field(default=10, ge=1, le=100)
    stage1_min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    final_method: Literal["local_sparse", "openai_compatible"] = "local_sparse"
    external_rerank_k: int = Field(default=30, ge=1, le=50)
    rerank_weight: float = Field(default=0.75, ge=0.0, le=1.0)
    min_selection_score: float = Field(default=0.15, ge=0.0, le=1.0)
    min_selection_margin: float = Field(default=0.02, ge=0.0, le=1.0)
    feature_dim: int = Field(default=32768, ge=1024, le=262144)
    ngram_min: int = Field(default=2, ge=1, le=5)
    ngram_max: int = Field(default=4, ge=1, le=6)
    max_total_candidate_chars: int = Field(default=5_000_000, ge=1_000, le=50_000_000)

    @model_validator(mode="after")
    def validate_request(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        if self.final_k > self.shortlist_k:
            raise ValueError("final_k must be <= shortlist_k")
        if self.external_rerank_k > self.shortlist_k:
            raise ValueError("external_rerank_k must be <= shortlist_k")
        ids = [item.id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        total_chars = sum(len(item.text) for item in self.candidates)
        if total_chars > self.max_total_candidate_chars:
            raise ValueError(
                f"candidate text is too large: {total_chars} chars exceeds max_total_candidate_chars={self.max_total_candidate_chars}"
            )
        return self


class CandidateDecisionItem(BaseModel):
    id: str
    rank: int
    stage1_score: float
    final_score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class DynamicCandidateResponse(BaseModel):
    selected_id: str | None
    requires_review: bool
    selection_signal: float
    score_margin: float
    candidates_received: int
    candidates_eligible: int
    candidates_shortlisted: int
    candidates_reranked: int
    results: list[CandidateDecisionItem]
    latency_ms: float
    stage1_method: str = "local_sparse_unicode_char_ngram"
    final_method: str
    calibration_note: str = "selection_signal is a routing heuristic, not an empirical probability of correctness"


class DynamicCandidateEngine:
    """Two-stage high-cardinality candidate selection.

    Stage 1 is always local and network-free. It hashes Unicode character n-grams and
    uses sparse cosine similarity to shortlist candidates. Optional stage 2 reranks a
    small bounded shortlist through RTDC's configured OpenAI-compatible provider.
    Candidate text is never persisted by this engine.
    """

    def __init__(self, operations: OperationalDecisionEngine):
        self.operations = operations

    @staticmethod
    def _metadata_match(metadata: dict[str, Any], required: dict[str, Any]) -> bool:
        return all(metadata.get(key) == value for key, value in required.items())

    async def select(self, request: DynamicCandidateRequest) -> DynamicCandidateResponse:
        started = time.perf_counter()
        eligible = [
            item for item in request.candidates
            if self._metadata_match(item.metadata, request.metadata_equals)
        ]
        if not eligible:
            return DynamicCandidateResponse(
                selected_id=None,
                requires_review=True,
                selection_signal=0.0,
                score_margin=0.0,
                candidates_received=len(request.candidates),
                candidates_eligible=0,
                candidates_shortlisted=0,
                candidates_reranked=0,
                results=[],
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
                final_method=request.final_method,
            )

        query_signature = sparse_signature(
            request.query, request.feature_dim, request.ngram_min, request.ngram_max
        )
        stage1: list[tuple[float, DynamicCandidate]] = []
        for candidate in eligible:
            signature = sparse_signature(
                candidate.text, request.feature_dim, request.ngram_min, request.ngram_max
            )
            score = sparse_cosine(query_signature, signature)
            if score >= request.stage1_min_score:
                stage1.append((score, candidate))

        stage1.sort(key=lambda row: (-row[0], row[1].id))
        shortlist = stage1[: request.shortlist_k]
        reranked_count = 0
        final_rows: list[tuple[float, float, DynamicCandidate]] = [
            (score, score, candidate) for score, candidate in shortlist
        ]

        if request.final_method == "openai_compatible" and shortlist:
            bounded = shortlist[: request.external_rerank_k]
            rank_request = RankRequest(
                query=request.query,
                candidates=[RankCandidate(id=item.id, text=item.text) for _, item in bounded],
                top_k=len(bounded),
                method="openai_compatible",
            )
            ranked = await self.operations.rank(rank_request)
            model_scores = {item.id: item.score for item in ranked.results}
            reranked_count = len(model_scores)
            blended: list[tuple[float, float, DynamicCandidate]] = []
            for stage1_score, candidate in shortlist:
                model_score = model_scores.get(candidate.id)
                if model_score is None:
                    final_score = stage1_score
                else:
                    final_score = (
                        (1.0 - request.rerank_weight) * stage1_score
                        + request.rerank_weight * model_score
                    )
                blended.append((final_score, stage1_score, candidate))
            final_rows = blended

        final_rows.sort(key=lambda row: (-row[0], -row[1], row[2].id))
        top = final_rows[: request.final_k]
        top_score = float(top[0][0]) if top else 0.0
        second_score = float(top[1][0]) if len(top) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        selected_id = top[0][2].id if top else None
        requires_review = (
            selected_id is None
            or top_score < request.min_selection_score
            or margin < request.min_selection_margin
        )

        return DynamicCandidateResponse(
            selected_id=selected_id,
            requires_review=requires_review,
            selection_signal=round(top_score, 6),
            score_margin=round(margin, 6),
            candidates_received=len(request.candidates),
            candidates_eligible=len(eligible),
            candidates_shortlisted=len(shortlist),
            candidates_reranked=reranked_count,
            results=[
                CandidateDecisionItem(
                    id=candidate.id,
                    rank=index + 1,
                    stage1_score=round(stage1_score, 6),
                    final_score=round(final_score, 6),
                    metadata=candidate.metadata,
                )
                for index, (final_score, stage1_score, candidate) in enumerate(top)
            ],
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            final_method=request.final_method,
        )
