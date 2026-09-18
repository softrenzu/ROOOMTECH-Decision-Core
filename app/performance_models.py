from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.advanced_models import MapReduceRequest


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
FastKind = Literal["decide", "detect", "route", "score", "verify", "features", "rank", "extract"]


class FastProfileCreate(BaseModel):
    profile_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=ID_PATTERN)
    kind: FastKind
    request: dict[str, Any] = Field(default_factory=dict)
    target_ms: float = Field(default=150.0, gt=0.0, le=10_000.0)
    prewarm: bool = True


class FastProfileSummary(BaseModel):
    profile_id: str
    kind: FastKind
    provider: str
    target_ms: float
    model_ids: list[str] = Field(default_factory=list)
    prewarmed: bool
    created_at: str


class FastDecisionRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100, pattern=ID_PATTERN)
    input: str = Field(min_length=1, max_length=200_000)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)


class FastDecisionResponse(BaseModel):
    request_id: str
    profile_id: str
    kind: str
    ok: bool
    data: Any = None
    error: str | None = None
    provider: str
    latency_ms: float
    queue_ms: float = 0.0
    execution_ms: float = 0.0
    target_ms: float
    within_target: bool


class LatencyStats(BaseModel):
    samples: int
    min_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    throughput_per_second: float
    target_ms: float | None = None
    target_hit_rate: float | None = None


class RealtimeLoadBenchmarkRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100, pattern=ID_PATTERN)
    inputs: list[str] = Field(min_length=1, max_length=10_000)
    repeat_runs: int = Field(default=10, ge=1, le=1000)
    concurrency: int = Field(default=32, ge=1, le=256)
    warmup_runs: int = Field(default=5, ge=0, le=100)

    @model_validator(mode="after")
    def cap_work(self):
        calls = len(self.inputs) * self.repeat_runs
        if calls > 100_000:
            raise ValueError("realtime benchmark is limited to 100,000 measured calls")
        return self


class RealtimeLoadBenchmarkResponse(BaseModel):
    profile_id: str
    calls: int
    concurrency: int
    errors: int
    wall_seconds: float
    latency: LatencyStats


class MapReduceLoadBenchmarkRequest(BaseModel):
    request: MapReduceRequest
    repeat_runs: int = Field(default=3, ge=1, le=20)
    warmup_runs: int = Field(default=0, ge=0, le=5)


class MapReduceRunMetric(BaseModel):
    run: int
    latency_ms: float
    items: int
    ok: int
    failed: int
    items_per_second: float


class MapReduceLoadBenchmarkResponse(BaseModel):
    runs: int
    items_per_run: int
    total_items: int
    total_failed: int
    wall_seconds: float
    latency: LatencyStats
    run_metrics: list[MapReduceRunMetric]
