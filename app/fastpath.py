from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.advanced_models import SchemaExtractionRequest
from app.models import DecisionRequest
from app.operation_models import (
    DetectionRequest,
    FeatureExtractionRequest,
    RankRequest,
    RouteRequest,
    ScoreRequest,
    VerificationRequest,
)
from app.performance_models import (
    FastDecisionRequest,
    FastDecisionResponse,
    FastProfileCreate,
    FastProfileSummary,
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


@dataclass
class CompiledProfile:
    summary: FastProfileSummary
    template: BaseModel


class FastPathEngine:
    """Prevalidated, network-free execution profiles for latency-sensitive decisions.

    Profiles intentionally disallow provider modes that can make external network calls.
    This makes the fast path predictable: rules, local classifiers, local n-gram ranking,
    or heuristic schema extraction only. The 150 ms figure is a target that is measured
    per request; it is not a hard guarantee.
    """

    def __init__(self, decision_engine, operations_engine, schema_extractor):
        self.decision_engine = decision_engine
        self.operations_engine = operations_engine
        self.schema_extractor = schema_extractor
        self.profile_limit = max(1, int(os.getenv("RTDC_FAST_PROFILE_LIMIT", "1000")))
        self._profiles: dict[str, CompiledProfile] = {}
        self._lock = asyncio.Lock()

    def _compile(self, spec: FastProfileCreate) -> tuple[BaseModel, str, list[str]]:
        config = dict(spec.request)
        kind = spec.kind
        model_ids: list[str] = []

        if kind == "decide":
            config.setdefault("input", "__fast_profile__")
            template = DecisionRequest.model_validate(config)
            provider = template.provider
            model_ids = [item.model_id for item in template.decisions if item.model_id]
            if provider == "local_classifier" and any(not item.model_id for item in template.decisions):
                raise ValueError("every decision requires model_id on a local_classifier fast profile")
        elif kind == "detect":
            config.setdefault("input", "__fast_profile__")
            template = DetectionRequest.model_validate(config)
            provider = template.provider
            model_ids = [template.model_id] if template.model_id else []
        elif kind == "route":
            config.setdefault("input", "__fast_profile__")
            template = RouteRequest.model_validate(config)
            provider = template.provider
            model_ids = [template.model_id] if template.model_id else []
        elif kind == "score":
            config.setdefault("input", "__fast_profile__")
            template = ScoreRequest.model_validate(config)
            provider = template.provider
            model_ids = [template.model_id] if template.model_id else []
        elif kind == "verify":
            config.setdefault("artifact", "__fast_profile__")
            template = VerificationRequest.model_validate(config)
            provider = template.provider
            model_ids = [template.model_id] if template.model_id else []
        elif kind == "features":
            config.setdefault("input", "__fast_profile__")
            template = FeatureExtractionRequest.model_validate(config)
            provider = template.provider
            model_ids = [item.model_id for item in template.features if item.model_id]
            if provider == "local_classifier" and any(not item.model_id for item in template.features):
                raise ValueError("every feature requires model_id on a local_classifier fast profile")
        elif kind == "rank":
            config.setdefault("query", "__fast_profile__")
            template = RankRequest.model_validate(config)
            if template.method != "local_ngram":
                raise ValueError("fast rank profiles require method=local_ngram")
            provider = "local_ngram"
        elif kind == "extract":
            config.setdefault("input", "__fast_profile__")
            template = SchemaExtractionRequest.model_validate(config)
            if template.provider != "heuristic":
                raise ValueError("fast extraction profiles require provider=heuristic")
            provider = "heuristic"
        else:
            raise ValueError(f"unsupported fast profile kind: {kind}")

        if provider in {"auto", "openai_compatible"}:
            raise ValueError("fast profiles cannot use auto or openai_compatible because they may make network calls")
        if provider == "local_classifier" and not model_ids:
            raise ValueError("local_classifier fast profile requires at least one model_id")
        return template, provider, list(dict.fromkeys(model_ids))

    async def create_profile(self, spec: FastProfileCreate) -> FastProfileSummary:
        template, provider, model_ids = self._compile(spec)
        prewarmed = False
        if spec.prewarm and model_ids:
            for model_id in model_ids:
                await asyncio.to_thread(self.decision_engine.local.predict_many, model_id, ["warmup"])
            prewarmed = True

        profile_id = spec.profile_id or "fp_" + uuid.uuid4().hex[:20]
        summary = FastProfileSummary(
            profile_id=profile_id,
            kind=spec.kind,
            provider=provider,
            target_ms=spec.target_ms,
            model_ids=model_ids,
            prewarmed=prewarmed,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        async with self._lock:
            if profile_id not in self._profiles and len(self._profiles) >= self.profile_limit:
                oldest = min(self._profiles.values(), key=lambda item: item.summary.created_at)
                self._profiles.pop(oldest.summary.profile_id, None)
            self._profiles[profile_id] = CompiledProfile(summary=summary, template=template)
        return summary

    def get_profile(self, profile_id: str) -> FastProfileSummary:
        profile = self._profiles.get(profile_id)
        if not profile:
            raise FileNotFoundError(f"fast profile not found: {profile_id}")
        return profile.summary

    def list_profiles(self) -> list[FastProfileSummary]:
        return [item.summary for item in sorted(self._profiles.values(), key=lambda value: value.summary.created_at)]

    async def delete_profile(self, profile_id: str) -> bool:
        async with self._lock:
            return self._profiles.pop(profile_id, None) is not None

    async def execute(self, request: FastDecisionRequest) -> FastDecisionResponse:
        profile = self._profiles.get(request.profile_id)
        if not profile:
            raise FileNotFoundError(f"fast profile not found: {request.profile_id}")

        started = time.perf_counter()
        ok = True
        error = None
        data: Any = None
        try:
            kind = profile.summary.kind
            template = profile.template
            if kind == "decide":
                data = _jsonable(await self.decision_engine.decide(template.model_copy(update={"input": request.input})))
            elif kind == "detect":
                data = _jsonable(await self.operations_engine.detect(template.model_copy(update={"input": request.input})))
            elif kind == "route":
                data = _jsonable(await self.operations_engine.route(template.model_copy(update={"input": request.input})))
            elif kind == "score":
                data = _jsonable(await self.operations_engine.score(template.model_copy(update={"input": request.input})))
            elif kind == "verify":
                data = _jsonable(await self.operations_engine.verify(template.model_copy(update={"artifact": request.input})))
            elif kind == "features":
                data = _jsonable(await self.operations_engine.extract_features(template.model_copy(update={"input": request.input})))
            elif kind == "rank":
                data = _jsonable(await self.operations_engine.rank(template.model_copy(update={"query": request.input})))
            elif kind == "extract":
                data = _jsonable(await self.schema_extractor.extract(template.model_copy(update={"input": request.input})))
            else:
                raise ValueError(f"unsupported fast profile kind: {kind}")
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return FastDecisionResponse(
            request_id=request.request_id,
            profile_id=request.profile_id,
            kind=profile.summary.kind,
            ok=ok,
            data=data,
            error=error,
            provider=profile.summary.provider,
            latency_ms=latency_ms,
            target_ms=profile.summary.target_ms,
            within_target=bool(ok and latency_ms <= profile.summary.target_ms),
        )
