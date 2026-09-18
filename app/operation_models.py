from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator

from app.models import DecisionResult

ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
ProviderName = Literal["auto", "rules", "local_classifier", "openai_compatible"]


class DetectionRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    property: str = Field(min_length=1, max_length=800)
    provider: ProviderName = "auto"
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    review_below: float = Field(default=0.65, ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class DetectionResponse(BaseModel):
    detected: bool
    probability: float
    threshold: float
    requires_review: bool
    result: DecisionResult


class RouteOption(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    description: str = Field(default="", max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class RouteRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    routes: list[RouteOption] = Field(min_length=2, max_length=30)
    provider: ProviderName = "auto"
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def unique_routes(self):
        ids = [item.id for item in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError("route ids must be unique")
        return self


class RouteResponse(BaseModel):
    route: str | None
    confidence: float
    requires_review: bool
    scores: dict[str, float]
    result: DecisionResult


class ScoreBand(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    value: float
    description: str = Field(default="", max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class ScoreRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    criterion: str = Field(min_length=1, max_length=800)
    bands: list[ScoreBand] = Field(min_length=2, max_length=20)
    provider: ProviderName = "auto"
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def unique_bands(self):
        ids = [item.id for item in self.bands]
        if len(ids) != len(set(ids)):
            raise ValueError("band ids must be unique")
        return self


class ScoreResponse(BaseModel):
    selected_band: str | None
    expected_score: float
    confidence: float
    requires_review: bool
    band_probabilities: dict[str, float]
    result: DecisionResult


class VerificationCheck(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    criterion: str = Field(min_length=1, max_length=1000)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    fail_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    review_below: float = Field(default=0.65, ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class VerificationRequest(BaseModel):
    artifact: str = Field(min_length=1, max_length=100_000)
    checks: list[VerificationCheck] = Field(min_length=1, max_length=30)
    provider: ProviderName = "auto"
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def unique_checks(self):
        ids = [item.id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("check ids must be unique")
        return self


class VerificationFinding(BaseModel):
    id: str
    severity: str
    status: Literal["pass", "fail", "review"]
    violation_probability: float
    confidence: float
    evidence: list[str] = Field(default_factory=list)


class VerificationResponse(BaseModel):
    passed: bool
    requires_review: bool
    findings: list[VerificationFinding]
    latency_ms: float


class RankCandidate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)


class RankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    candidates: list[RankCandidate] = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=100)
    method: Literal["local_ngram", "openai_compatible"] = "local_ngram"
    feature_dim: int = Field(default=2048, ge=256, le=16384)
    ngram_min: int = Field(default=1, ge=1, le=5)
    ngram_max: int = Field(default=4, ge=1, le=6)

    @model_validator(mode="after")
    def validate_rank_request(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        ids = [item.id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        return self


class RankedItem(BaseModel):
    id: str
    score: float
    rank: int


class RankResponse(BaseModel):
    results: list[RankedItem]
    method: str
    latency_ms: float
    candidates_scored: int


class FeatureSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    question: str = Field(min_length=1, max_length=1000)
    choices: list[str] = Field(min_length=2, max_length=20)
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_feature(self):
        if len(self.choices) != len(set(self.choices)):
            raise ValueError("feature choices must be unique")
        unknown = set(self.keywords) - set(self.choices)
        if unknown:
            raise ValueError(f"keywords contain unknown feature choices: {sorted(unknown)}")
        return self


class FeatureExtractionRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    features: list[FeatureSpec] = Field(min_length=1, max_length=30)
    provider: ProviderName = "auto"

    @model_validator(mode="after")
    def unique_features(self):
        ids = [item.id for item in self.features]
        if len(ids) != len(set(ids)):
            raise ValueError("feature ids must be unique")
        return self


class FeatureExtractionResponse(BaseModel):
    selected: dict[str, str | None]
    probabilities: dict[str, dict[str, float]]
    requires_review: dict[str, bool]
    results: list[DecisionResult]
    latency_ms: float
