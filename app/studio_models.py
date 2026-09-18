from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import DecisionRequest, DecisionResponse


class CalibrationSample(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    correct: bool


class ReliabilityBin(BaseModel):
    lower: float
    upper: float
    count: int
    mean_confidence: float | None = None
    accuracy: float | None = None
    gap: float | None = None


class ThresholdPoint(BaseModel):
    threshold: float
    accepted: int
    coverage: float
    accuracy: float | None = None
    error_rate: float | None = None


class CalibrationRequest(BaseModel):
    samples: list[CalibrationSample] = Field(min_length=20, max_length=200_000)
    target_max_error_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    min_accepted_samples: int = Field(default=20, ge=1, le=200_000)
    bins: int = Field(default=10, ge=2, le=50)


class CalibrationResponse(BaseModel):
    samples: int
    accuracy: float
    mean_confidence: float
    expected_calibration_error: float
    brier_score: float
    target_max_error_rate: float
    recommended_threshold: float | None
    accepted_samples: int
    coverage_at_threshold: float
    accuracy_at_threshold: float | None
    reliability: list[ReliabilityBin]
    threshold_curve: list[ThresholdPoint]
    methodology: str = "empirical held-out outcomes; no third-party model outputs required"


class ReviewPolicy(BaseModel):
    review_below_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    review_if_requires_review: bool = True
    review_if_abstained: bool = True
    store_input: bool = False
    retention_days: int = Field(default=30, ge=1, le=3650)


class ReviewCreate(BaseModel):
    source: str = Field(default="manual", min_length=1, max_length=80)
    decision_id: str = Field(min_length=1, max_length=120)
    input_text: str | None = Field(default=None, max_length=100_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    model_output: dict[str, Any] = Field(default_factory=dict)
    suggested_label: str | None = Field(default=None, max_length=500)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    external_ref: str | None = Field(default=None, max_length=500)
    store_input: bool = False
    retention_days: int = Field(default=30, ge=1, le=3650)


class ReviewItem(BaseModel):
    id: str
    created_at: str
    updated_at: str
    status: Literal["pending", "resolved", "dismissed"]
    source: str
    decision_id: str
    input_text: str | None
    input_sha256: str | None
    payload: dict[str, Any]
    model_output: dict[str, Any]
    suggested_label: str | None
    confidence: float | None
    external_ref: str | None
    resolved_label: str | None
    reviewer: str | None
    notes: str | None
    retention_until: str


class ReviewResolve(BaseModel):
    status: Literal["resolved", "dismissed"] = "resolved"
    resolved_label: str | None = Field(default=None, max_length=500)
    reviewer: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)


class GovernedDecisionRequest(BaseModel):
    decision: DecisionRequest
    policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    external_ref: str | None = Field(default=None, max_length=500)


class GovernedDecisionResponse(BaseModel):
    decision: DecisionResponse
    routed_to_review: bool
    review_ids: list[str] = Field(default_factory=list)
