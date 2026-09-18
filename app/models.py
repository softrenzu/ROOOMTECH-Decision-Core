from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class DecisionSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    question: str = Field(min_length=1, max_length=1000)
    choices: list[str] = Field(min_length=2, max_length=50)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    min_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    max_entropy: float = Field(default=0.85, ge=0.0, le=1.0)
    allow_abstain: bool = True
    keywords: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_choices(self):
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must be unique")
        unknown = set(self.keywords) - set(self.choices)
        if unknown:
            raise ValueError(f"keywords contain unknown choices: {sorted(unknown)}")
        return self


class DecisionRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    decisions: list[DecisionSpec] = Field(min_length=1, max_length=30)
    provider: Literal["auto", "rules", "openai_compatible"] = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChoiceScore(BaseModel):
    choice: str
    probability: float = Field(ge=0.0, le=1.0)


class DecisionResult(BaseModel):
    id: str
    selected: str | None
    scores: list[ChoiceScore]
    confidence: float
    margin: float
    entropy: float
    requires_review: bool
    abstained: bool
    provider: str
    evidence: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class DecisionResponse(BaseModel):
    results: list[DecisionResult]
    latency_ms: float
    model_called: bool


class BatchRequest(BaseModel):
    items: list[DecisionRequest] = Field(min_length=1, max_length=100)


class BatchResponse(BaseModel):
    items: list[DecisionResponse]
