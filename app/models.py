from __future__ import annotations

from collections import Counter
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


MODEL_ID_PATTERN = r"^[A-Za-z0-9_.-]+$"


class DecisionSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=MODEL_ID_PATTERN)
    question: str = Field(min_length=1, max_length=1000)
    choices: list[str] = Field(min_length=2, max_length=50)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    min_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    max_entropy: float = Field(default=0.85, ge=0.0, le=1.0)
    allow_abstain: bool = True
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    model_id: str | None = Field(default=None, max_length=100, pattern=MODEL_ID_PATTERN)

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
    provider: Literal["auto", "rules", "local_classifier", "openai_compatible"] = "auto"
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


class TrainingExample(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    label: str = Field(min_length=1, max_length=200)


class TrainModelRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=80, pattern=MODEL_ID_PATTERN)
    examples: list[TrainingExample] = Field(min_length=6, max_length=5000)
    model_name: str | None = Field(default=None, max_length=120)
    feature_dim: int = Field(default=4096, ge=256, le=65536)
    ngram_min: int = Field(default=1, ge=1, le=5)
    ngram_max: int = Field(default=3, ge=1, le=6)
    epochs: int = Field(default=40, ge=1, le=500)
    learning_rate: float = Field(default=0.03, gt=0.0, le=1.0)
    batch_size: int = Field(default=128, ge=8, le=2048)
    validation_split: float = Field(default=0.2, ge=0.0, le=0.4)
    device: Literal["auto", "cpu", "cuda"] = "auto"

    @model_validator(mode="after")
    def validate_dataset(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        counts = Counter(item.label for item in self.examples)
        if len(counts) < 2:
            raise ValueError("training requires at least two labels")
        too_small = [label for label, count in counts.items() if count < 2]
        if too_small:
            raise ValueError(f"each label needs at least two examples: {too_small}")
        return self


class TrainModelResponse(BaseModel):
    model_id: str
    decision_id: str
    labels: list[str]
    examples: int
    device: str
    feature_dim: int
    ngram_range: tuple[int, int]
    temperature: float
    train_accuracy: float
    validation_accuracy: float | None
    raw_text_persisted: bool = False


class LocalModelSummary(BaseModel):
    model_id: str
    decision_id: str
    model_name: str | None = None
    labels: list[str]
    created_at: str
    examples: int
    feature_dim: int
    ngram_min: int
    ngram_max: int
    temperature: float


class LocalPredictRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=1000)
    device: Literal["auto", "cpu", "cuda"] = "auto"


class LocalPrediction(BaseModel):
    selected: str
    confidence: float
    scores: dict[str, float]


class LocalPredictResponse(BaseModel):
    model_id: str
    device: str
    predictions: list[LocalPrediction]
