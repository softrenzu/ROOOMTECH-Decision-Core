from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.engine import DecisionEngine
from app.models import DecisionResult, DecisionSpec
from app.parallel_head_model import ParallelHeadModelProvider, ParallelHeadPredictRequest


ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
JudgmentKind = Literal["boolean", "categorical", "scalar"]
RoutingMode = Literal["auto", "local_only", "external_only"]


class JudgmentOption(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    description: str = Field(default="", max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class JudgmentLevel(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    value: float
    description: str = Field(default="", max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class JudgmentSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    kind: JudgmentKind
    instruction: str = Field(min_length=1, max_length=1200)
    options: list[JudgmentOption] = Field(default_factory=list, max_length=50)
    levels: list[JudgmentLevel] = Field(default_factory=list, max_length=20)
    true_keywords: list[str] = Field(default_factory=list, max_length=100)
    false_keywords: list[str] = Field(default_factory=list, max_length=100)
    model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    min_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    max_entropy: float = Field(default=0.85, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "boolean":
            if self.options or self.levels:
                raise ValueError("boolean judgments do not use options or levels")
        elif self.kind == "categorical":
            if len(self.options) < 2:
                raise ValueError("categorical judgments require at least two options")
            ids = [item.id for item in self.options]
            if len(ids) != len(set(ids)):
                raise ValueError("categorical option ids must be unique")
            if self.levels:
                raise ValueError("categorical judgments do not use levels")
        elif self.kind == "scalar":
            if len(self.levels) < 2:
                raise ValueError("scalar judgments require at least two levels")
            ids = [item.id for item in self.levels]
            if len(ids) != len(set(ids)):
                raise ValueError("scalar level ids must be unique")
            if self.options:
                raise ValueError("scalar judgments do not use options")
        return self

    def decision_spec(self) -> DecisionSpec:
        if self.kind == "boolean":
            choices = ["true", "false"]
            keywords = {"true": self.true_keywords, "false": self.false_keywords}
        elif self.kind == "categorical":
            choices = [item.id for item in self.options]
            keywords = {item.id: item.keywords for item in self.options}
        else:
            choices = [item.id for item in self.levels]
            keywords = {item.id: item.keywords for item in self.levels}
        return DecisionSpec(
            id=self.id,
            question=self.instruction,
            choices=choices,
            min_confidence=self.min_confidence,
            min_margin=self.min_margin,
            max_entropy=self.max_entropy,
            allow_abstain=True,
            keywords=keywords,
            model_id=self.model_id,
        )


class DecisionAcceleratorRequest(BaseModel):
    state: str = Field(min_length=1, max_length=200_000)
    judgments: list[JudgmentSpec] = Field(min_length=1, max_length=64)
    routing_mode: RoutingMode = "auto"
    parallel_model_id: str | None = Field(default=None, max_length=100, pattern=ID_PATTERN)
    max_external_judgments: int = Field(default=16, ge=0, le=64)
    external_weight: float = Field(default=0.85, ge=0.0, le=1.0)
    deadline_ms: int | None = Field(default=None, ge=10, le=120_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.judgments]
        if len(ids) != len(set(ids)):
            raise ValueError("judgment ids must be unique")
        if self.routing_mode == "external_only" and len(self.judgments) > self.max_external_judgments:
            raise ValueError("external_only requires max_external_judgments >= number of judgments")
        return self


class JudgmentResult(BaseModel):
    id: str
    kind: JudgmentKind
    selected: bool | str | None = None
    expected_value: float | None = None
    probabilities: dict[str, float]
    confidence: float
    margin: float
    entropy: float
    requires_review: bool
    abstained: bool
    provider: str
    external_escalated: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class AcceleratorRoutingStats(BaseModel):
    judgment_count: int
    parallel_model_heads: int
    local_model_tasks: int
    rules_only_baselines: int
    locally_resolved: int
    unresolved_before_external: int
    external_judgments: int
    external_call_count: int
    external_timeout: bool
    review_count: int
    state_chars: int
    execution_shape: str = "shared_encoder_parallel_heads_then_batched_external"


class DecisionAcceleratorResponse(BaseModel):
    results: list[JudgmentResult]
    routing: AcceleratorRoutingStats
    latency_ms: float


class DecisionAccelerator:
    """Machine-oriented typed judgments with bounded automatic LLM fallback.

    An optional RTDC-owned shared-encoder model can emit many probability heads in
    one forward pass. Remaining per-judgment local models run concurrently. Only
    uncertain judgments are eligible for one bounded external-model batch. The API
    never exposes chain-of-thought.
    """

    def __init__(self, engine: DecisionEngine, parallel_models: ParallelHeadModelProvider | None = None):
        self.engine = engine
        self.parallel_models = parallel_models

    @staticmethod
    def _priority(result: DecisionResult) -> float:
        return (1.0 - result.confidence) + 0.5 * result.entropy + 0.5 * (1.0 - max(0.0, result.margin))

    @staticmethod
    def _convert(spec: JudgmentSpec, result: DecisionResult, external_escalated: bool) -> JudgmentResult:
        probs = {item.choice: item.probability for item in result.scores}
        selected: bool | str | None
        expected_value = None
        if spec.kind == "boolean":
            selected = None if result.selected is None else result.selected == "true"
        elif spec.kind == "categorical":
            selected = result.selected
        else:
            selected = result.selected
            values = {level.id: level.value for level in spec.levels}
            expected_value = sum(values.get(key, 0.0) * probability for key, probability in probs.items())
        return JudgmentResult(
            id=spec.id,
            kind=spec.kind,
            selected=selected,
            expected_value=round(expected_value, 6) if expected_value is not None else None,
            probabilities=probs,
            confidence=result.confidence,
            margin=result.margin,
            entropy=result.entropy,
            requires_review=result.requires_review,
            abstained=result.abstained,
            provider=result.provider,
            external_escalated=external_escalated,
            reason_codes=result.reason_codes,
        )

    async def evaluate(self, request: DecisionAcceleratorRequest, project_id: str = "local") -> DecisionAcceleratorResponse:
        started = time.perf_counter()
        specs = [judgment.decision_spec() for judgment in request.judgments]
        by_id = {judgment.id: judgment for judgment in request.judgments}
        rules_data = self.engine.rules.evaluate(request.state, specs)
        parallel_data: dict[str, dict] = {}
        local_data: dict[str, dict] = {}
        parallel_model_heads = 0
        local_model_tasks = 0

        if request.routing_mode != "external_only" and request.parallel_model_id:
            if self.parallel_models is None:
                raise ValueError("parallel_model_id was supplied but the parallel model provider is unavailable")
            summary = self.parallel_models.get_model(project_id, request.parallel_model_id)
            model_head_ids = {head.id for head in summary.heads}
            matching = [spec.id for spec in specs if spec.id in model_head_ids]
            if matching:
                prediction = await self.parallel_models.predict_async(
                    project_id,
                    request.parallel_model_id,
                    ParallelHeadPredictRequest(text=request.state, heads=matching),
                )
                parallel_model_heads = len(prediction.predictions)
                for item in prediction.predictions:
                    parallel_data[item.id] = {
                        "scores": item.probabilities,
                        "evidence": [],
                        "reason_codes": ["PARALLEL_SHARED_ENCODER", "CALIBRATED_HEAD_TEMPERATURE"],
                    }

        async def local_one(spec: DecisionSpec):
            try:
                data = await self.engine.local.evaluate_one_async(spec.model_id, request.state, spec.choices)
                return spec.id, data
            except Exception:
                return spec.id, None

        local_specs = [spec for spec in specs if spec.id not in parallel_data and spec.model_id]
        if request.routing_mode != "external_only" and local_specs:
            local_model_tasks = len(local_specs)
            rows = await asyncio.gather(*(local_one(spec) for spec in local_specs))
            local_data = {spec_id: data for spec_id, data in rows if data is not None}

        baseline_results: dict[str, DecisionResult] = {}
        baseline_raw: dict[str, dict] = {}
        rules_only = 0
        for spec in specs:
            if request.routing_mode == "external_only":
                continue
            if spec.id in parallel_data:
                data = parallel_data[spec.id]
                provider = "parallel_head_model"
            elif spec.id in local_data:
                data = local_data[spec.id]
                provider = "local_classifier"
            else:
                data = rules_data[spec.id]
                provider = "rules"
                rules_only += 1
            baseline_raw[spec.id] = data
            baseline_results[spec.id] = self.engine._result(spec, data, provider)

        unresolved: list[DecisionSpec]
        if request.routing_mode == "external_only":
            unresolved = list(specs)
        else:
            unresolved = [spec for spec in specs if baseline_results[spec.id].requires_review]
        unresolved_before = len(unresolved)

        external_specs: list[DecisionSpec] = []
        if request.routing_mode != "local_only" and self.engine.model.configured and request.max_external_judgments > 0:
            if request.routing_mode == "external_only":
                external_specs = unresolved[: request.max_external_judgments]
            else:
                external_specs = sorted(
                    unresolved,
                    key=lambda spec: self._priority(baseline_results[spec.id]),
                    reverse=True,
                )[: request.max_external_judgments]

        external_data: dict[str, dict] = {}
        external_timeout = False
        external_call_count = 0
        if external_specs:
            external_call_count = 1
            coro = self.engine.model.evaluate(request.state, external_specs)
            if request.deadline_ms is None:
                external_data = await coro
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                remaining = max(0.001, (request.deadline_ms - elapsed_ms) / 1000.0)
                try:
                    external_data = await asyncio.wait_for(coro, timeout=remaining)
                except asyncio.TimeoutError:
                    external_timeout = True
                    external_data = {}

        final_results: list[JudgmentResult] = []
        external_ids = set(external_data)
        for spec in specs:
            if request.routing_mode == "external_only":
                data = external_data.get(spec.id)
                if data is not None:
                    decision = self.engine._result(spec, data, "openai_compatible")
                else:
                    decision = self.engine._result(spec, {"scores": {}}, "external_unavailable")
                final_results.append(self._convert(by_id[spec.id], decision, spec.id in external_ids))
                continue

            baseline = baseline_results[spec.id]
            if spec.id in external_data:
                local_raw = baseline_raw[spec.id]
                merged = {
                    "scores": self.engine._blend(
                        spec,
                        local_raw.get("scores", {}),
                        external_data[spec.id].get("scores", {}),
                        model_weight=request.external_weight,
                    ),
                    "evidence": list(dict.fromkeys(external_data[spec.id].get("evidence", []) + local_raw.get("evidence", [])))[:3],
                    "reason_codes": ["EXTERNAL_FALLBACK"] + external_data[spec.id].get("reason_codes", []),
                }
                decision = self.engine._result(spec, merged, f"accelerated_{baseline.provider}_external")
            else:
                decision = baseline
            final_results.append(self._convert(by_id[spec.id], decision, spec.id in external_ids))

        locally_resolved = sum(1 for result in baseline_results.values() if not result.requires_review)
        review_count = sum(1 for result in final_results if result.requires_review)
        return DecisionAcceleratorResponse(
            results=final_results,
            routing=AcceleratorRoutingStats(
                judgment_count=len(specs),
                parallel_model_heads=parallel_model_heads,
                local_model_tasks=local_model_tasks,
                rules_only_baselines=rules_only,
                locally_resolved=locally_resolved,
                unresolved_before_external=unresolved_before,
                external_judgments=len(external_specs),
                external_call_count=external_call_count,
                external_timeout=external_timeout,
                review_count=review_count,
                state_chars=len(request.state),
            ),
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
