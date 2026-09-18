from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SchemaExtractionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    input: str = Field(min_length=1, max_length=200_000)
    json_schema: dict[str, Any] = Field(alias="schema")
    provider: Literal["auto", "heuristic", "openai_compatible"] = "auto"
    strict: bool = True

    @model_validator(mode="after")
    def validate_schema_size(self):
        encoded = json.dumps(self.json_schema, ensure_ascii=False)
        if len(encoded) > 100_000:
            raise ValueError("schema is too large")
        return self


class SchemaExtractionResponse(BaseModel):
    data: Any
    valid: bool
    validation_errors: list[str] = Field(default_factory=list)
    provider: str
    latency_ms: float


class MapTask(BaseModel):
    kind: Literal["decide", "detect", "route", "score", "verify", "features", "extract"]
    config: dict[str, Any] = Field(default_factory=dict)
    input_field: str = Field(default="text", min_length=1, max_length=200)


class ReduceSpec(BaseModel):
    kind: Literal["collect", "count_by", "sum", "avg", "min", "max", "top_k"] = "collect"
    path: str | None = Field(default=None, max_length=300)
    top_k: int = Field(default=10, ge=1, le=1000)

    @model_validator(mode="after")
    def require_path_for_reducers(self):
        if self.kind != "collect" and not self.path:
            raise ValueError("reduce.path is required for this reducer")
        return self


class MapReduceRequest(BaseModel):
    items: list[Any] = Field(min_length=1, max_length=100_000)
    map: MapTask
    reduce: ReduceSpec = Field(default_factory=ReduceSpec)
    concurrency: int = Field(default=16, ge=1, le=256)
    fail_fast: bool = False
    include_map_results: bool = True


class MapItemResult(BaseModel):
    index: int
    ok: bool
    value: Any = None
    error: str | None = None


class MapReduceResponse(BaseModel):
    items: int
    ok: int
    failed: int
    reduced: Any
    results: list[MapItemResult] = Field(default_factory=list)
    latency_ms: float


class MapReduceJobSubmission(BaseModel):
    job_id: str
    state: Literal["queued", "running", "completed", "failed"]
    total_shards: int
    total_items: int


class MapReduceJobStatus(BaseModel):
    job_id: str
    state: str
    total_shards: int
    completed_shards: int
    total_items: int
    result: dict[str, Any] | None = None
    error: str | None = None


class RealtimeEvent(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)
    kind: Literal["ping", "decide", "detect", "route", "score", "verify", "features", "extract"]
    request: dict[str, Any] = Field(default_factory=dict)


class RealtimeBatchRequest(BaseModel):
    events: list[RealtimeEvent] = Field(min_length=1, max_length=1000)
    concurrency: int = Field(default=16, ge=1, le=128)


class RealtimeResult(BaseModel):
    request_id: str
    kind: str
    ok: bool
    data: Any = None
    error: str | None = None
    latency_ms: float
