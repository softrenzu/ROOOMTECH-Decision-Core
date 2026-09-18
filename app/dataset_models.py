from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models import BenchmarkResponse, TrainModelResponse


DATASET_ID_PATTERN = r"^[A-Za-z0-9_.-]+$"


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    decision_id: str = Field(min_length=1, max_length=80, pattern=DATASET_ID_PATTERN)
    description: str | None = Field(default=None, max_length=2000)
    source_type: Literal[
        "operator_owned",
        "consented",
        "licensed",
        "synthetic",
        "internal_business_records",
        "public_domain",
    ]
    provenance: str = Field(min_length=3, max_length=4000)
    purpose: str = Field(default="decision-model training and evaluation", min_length=3, max_length=1000)
    rights_attested: bool = False
    contains_personal_data: bool = False
    allow_model_training: bool = True
    retention_days: int = Field(default=365, ge=1, le=3650)

    @model_validator(mode="after")
    def require_rights_attestation(self):
        if not self.rights_attested:
            raise ValueError("rights_attested must be true before a dataset can be created")
        return self


class DatasetExampleInput(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    label: str = Field(min_length=1, max_length=200)
    split: Literal["train", "validation", "test"] = "train"
    source_ref: str | None = Field(default=None, max_length=1000)


class DatasetExamplesAdd(BaseModel):
    examples: list[DatasetExampleInput] = Field(min_length=1, max_length=20_000)


class DatasetExample(BaseModel):
    id: str
    dataset_id: str
    created_at: str
    text: str
    text_sha256: str
    label: str
    split: Literal["train", "validation", "test"]
    source_ref: str | None = None
    review_id: str | None = None


class DatasetSummary(BaseModel):
    id: str
    name: str
    decision_id: str
    description: str | None
    source_type: str
    provenance: str
    purpose: str
    rights_attested: bool
    contains_personal_data: bool
    allow_model_training: bool
    created_at: str
    updated_at: str
    retention_until: str
    examples: int = 0
    train_examples: int = 0
    validation_examples: int = 0
    test_examples: int = 0
    labels: dict[str, int] = Field(default_factory=dict)
    fingerprint_sha256: str


class DatasetImportReviewsRequest(BaseModel):
    split: Literal["train", "validation"] = "train"
    limit: int = Field(default=5000, ge=1, le=50_000)


class DatasetImportResult(BaseModel):
    considered: int
    added: int
    skipped_duplicates: int
    skipped_without_raw_input: int = 0


class DatasetTrainRequest(BaseModel):
    model_name: str | None = Field(default=None, max_length=120)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    feature_dim: int = Field(default=4096, ge=256, le=65536)
    ngram_min: int = Field(default=1, ge=1, le=5)
    ngram_max: int = Field(default=3, ge=1, le=6)
    epochs: int = Field(default=40, ge=1, le=500)
    learning_rate: float = Field(default=0.03, gt=0.0, le=1.0)
    batch_size: int = Field(default=128, ge=8, le=2048)
    internal_validation_split: float = Field(default=0.2, ge=0.0, le=0.4)
    evaluate_on: Literal["validation", "test", "none"] = "validation"
    confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    benchmark_batch_size: int = Field(default=32, ge=1, le=2048)
    benchmark_repeat_runs: int = Field(default=3, ge=1, le=25)

    @model_validator(mode="after")
    def validate_ngram_range(self):
        if self.ngram_max < self.ngram_min:
            raise ValueError("ngram_max must be >= ngram_min")
        return self


class DatasetModelRecord(BaseModel):
    id: str
    dataset_id: str
    model_id: str
    created_at: str
    train_examples: int
    dataset_fingerprint_sha256: str
    training: dict
    evaluation: dict | None = None


class DatasetTrainResponse(BaseModel):
    dataset_id: str
    dataset_fingerprint_sha256: str
    training: TrainModelResponse
    evaluation: BenchmarkResponse | None = None
    lineage_id: str


class ActiveLearningCandidate(BaseModel):
    review_id: str
    decision_id: str
    created_at: str
    confidence: float | None
    uncertainty: float
    suggested_label: str | None
    input_text: str | None
    input_sha256: str | None
    external_ref: str | None
