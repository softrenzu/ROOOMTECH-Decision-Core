from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models import LocalPrediction


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
ProjectScope = Literal["inference", "guardrails", "realtime", "datasets", "reviews", "models", "web", "graphs", "candidates"]
DeploymentEnvironment = Literal["development", "staging", "production"]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    request_quota_per_day: int = Field(default=100_000, ge=1, le=1_000_000_000)
    enabled: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    request_quota_per_day: int | None = Field(default=None, ge=1, le=1_000_000_000)
    enabled: bool | None = None


class ProjectSummary(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    request_quota_per_day: int
    enabled: bool
    requests_today: int = 0


class ProjectKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    scopes: list[ProjectScope] = Field(default_factory=lambda: ["inference"], min_length=1, max_length=9)
    expires_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def unique_scopes(self):
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("scopes must be unique")
        return self


class ProjectKeySummary(BaseModel):
    id: str
    project_id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None


class ProjectKeyIssued(ProjectKeySummary):
    key: str
    warning: str = "This key is shown once. Store it in a secret manager."


class AuthContext(BaseModel):
    project_id: str
    key_id: str
    scopes: list[str]
    request_quota_per_day: int
    requests_today: int


class AuditEvent(BaseModel):
    id: str
    created_at: str
    project_id: str | None = None
    key_id: str | None = None
    method: str
    path: str
    status_code: int
    latency_ms: float
    request_id: str | None = None


class ModelPromotionRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    model_id: str = Field(min_length=1, max_length=100, pattern=ID_PATTERN)
    environment: DeploymentEnvironment = "production"
    note: str | None = Field(default=None, max_length=1000)


class ModelDeploymentSummary(BaseModel):
    project_id: str
    decision_id: str
    environment: DeploymentEnvironment
    model_id: str
    version: int
    promoted_at: str
    promoted_by_key_id: str | None = None
    note: str | None = None


class ModelRollbackRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    environment: DeploymentEnvironment = "production"


class ModelRollbackResponse(BaseModel):
    deployment: ModelDeploymentSummary
    rolled_back_from_model_id: str


class DeployedPredictRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    input: str = Field(min_length=1, max_length=100_000)
    environment: DeploymentEnvironment = "production"
    device: Literal["auto", "cpu", "cuda"] = "auto"


class DeployedPredictResponse(BaseModel):
    project_id: str
    decision_id: str
    environment: DeploymentEnvironment
    deployment_version: int
    model_id: str
    device: str
    prediction: LocalPrediction
    latency_ms: float
    quota_remaining_today: int
