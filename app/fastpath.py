from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import suppress
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
from app.profile_registry import SharedFastProfileRegistry


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


class FastPathOverloaded(RuntimeError):
    """Raised before execution when the bounded realtime scheduler cannot accept work."""

    def __init__(self, reason: str, queue_depth: int, capacity: int):
        self.reason = reason
        self.queue_depth = queue_depth
        self.capacity = capacity
        super().__init__(f"fast path overloaded: {reason}; queue_depth={queue_depth}; capacity={capacity}")


@dataclass
class CompiledProfile:
    summary: FastProfileSummary
    template: BaseModel


class FastPathEngine:
    """Prevalidated, network-free execution profiles for latency-sensitive decisions.

    The fast path uses bounded concurrent worker slots. Requests above the configured
    pending capacity are rejected instead of allowing an unbounded latency queue. Local
    classifier inference uses accelerator-aware scheduling. With the optional Redis
    registry, profile definitions are shared and process-local compiled copies are
    invalidated over Pub/Sub when another worker updates or deletes a profile.
    """

    def __init__(self, decision_engine, operations_engine, schema_extractor):
        self.decision_engine = decision_engine
        self.operations_engine = operations_engine
        self.schema_extractor = schema_extractor
        self.registry = SharedFastProfileRegistry()
        self.profile_limit = max(1, int(os.getenv("RTDC_FAST_PROFILE_LIMIT", "1000")))
        self.worker_slots = max(1, int(os.getenv("RTDC_FAST_WORKERS", "32")))
        self.queue_capacity = max(self.worker_slots, int(os.getenv("RTDC_FAST_QUEUE_CAPACITY", "4096")))
        self.max_queue_wait_ms = max(0.0, float(os.getenv("RTDC_FAST_MAX_QUEUE_WAIT_MS", "100")))
        self._profiles: dict[str, CompiledProfile] = {}
        self._profile_load_locks: dict[str, asyncio.Lock] = {}
        self._registry_listener_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(self.worker_slots)
        self._pending = 0
        self._inflight = 0
        self._rejected = 0
        self._queue_timeouts = 0
        self._completed = 0
        self._shared_profile_loads = 0
        self._shared_profile_invalidations = 0

    async def start(self) -> None:
        if self.registry.configured and (self._registry_listener_task is None or self._registry_listener_task.done()):
            self._registry_listener_task = asyncio.create_task(
                self.registry.listen(self._on_shared_profile_event),
                name="rtdc-fast-profile-registry",
            )
            await asyncio.sleep(0)

    async def stop(self) -> None:
        task = self._registry_listener_task
        self._registry_listener_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _on_shared_profile_event(self, action: str, profile_id: str) -> None:
        if action not in {"upsert", "delete"}:
            return
        async with self._lock:
            if self._profiles.pop(profile_id, None) is not None:
                self._shared_profile_invalidations += 1

    def runtime_info(self) -> dict[str, int | float | bool]:
        return {
            "workers": self.worker_slots,
            "pending_capacity": self.queue_capacity,
            "pending": self._pending,
            "inflight": self._inflight,
            "queue_depth": max(0, self._pending - self._inflight),
            "max_queue_wait_ms": self.max_queue_wait_ms,
            "rejected": self._rejected,
            "queue_timeouts": self._queue_timeouts,
            "completed": self._completed,
            "shared_profile_registry": self.registry.configured,
            "shared_profile_listener": bool(self._registry_listener_task and not self._registry_listener_task.done()),
            "shared_profile_loads": self._shared_profile_loads,
            "shared_profile_invalidations": self._shared_profile_invalidations,
        }

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

    async def _install_profile(
        self,
        spec: FastProfileCreate,
        *,
        persist: bool,
        stored_summary: FastProfileSummary | None = None,
    ) -> FastProfileSummary:
        template, provider, model_ids = self._compile(spec)
        prewarmed = False
        if spec.prewarm and model_ids:
            for model_id in model_ids:
                await asyncio.to_thread(self.decision_engine.local.predict_many, model_id, ["warmup"])
            prewarmed = True

        profile_id = spec.profile_id or "fp_" + uuid.uuid4().hex[:20]
        created_at = stored_summary.created_at if stored_summary else datetime.now(timezone.utc).isoformat()
        summary = FastProfileSummary(
            profile_id=profile_id,
            kind=spec.kind,
            provider=provider,
            target_ms=spec.target_ms,
            model_ids=model_ids,
            prewarmed=prewarmed,
            created_at=created_at,
        )

        if persist and self.registry.configured:
            persisted_spec = spec.model_copy(update={"profile_id": profile_id})
            await self.registry.save(persisted_spec, summary)

        async with self._lock:
            if profile_id not in self._profiles and len(self._profiles) >= self.profile_limit:
                oldest = min(self._profiles.values(), key=lambda item: item.summary.created_at)
                self._profiles.pop(oldest.summary.profile_id, None)
            self._profiles[profile_id] = CompiledProfile(summary=summary, template=template)
        return summary

    async def create_profile(self, spec: FastProfileCreate) -> FastProfileSummary:
        return await self._install_profile(spec, persist=True)

    async def _load_shared_profile(self, profile_id: str) -> CompiledProfile | None:
        if not self.registry.configured:
            return None

        async with self._lock:
            existing = self._profiles.get(profile_id)
            if existing is not None:
                return existing
            load_lock = self._profile_load_locks.setdefault(profile_id, asyncio.Lock())

        async with load_lock:
            async with self._lock:
                existing = self._profiles.get(profile_id)
                if existing is not None:
                    return existing
            row = await self.registry.get(profile_id)
            if row is None:
                return None
            spec, stored_summary = row
            await self._install_profile(spec, persist=False, stored_summary=stored_summary)
            self._shared_profile_loads += 1
            return self._profiles.get(profile_id)

    def get_profile(self, profile_id: str) -> FastProfileSummary:
        profile = self._profiles.get(profile_id)
        if not profile:
            raise FileNotFoundError(f"fast profile not found: {profile_id}")
        return profile.summary

    def list_profiles(self) -> list[FastProfileSummary]:
        return [item.summary for item in sorted(self._profiles.values(), key=lambda value: value.summary.created_at)]

    async def list_profiles_shared(self) -> list[FastProfileSummary]:
        by_id = {item.profile_id: item for item in self.list_profiles()}
        if self.registry.configured:
            for _, summary in await self.registry.list():
                by_id.setdefault(summary.profile_id, summary)
        return sorted(by_id.values(), key=lambda item: item.created_at)

    async def delete_profile(self, profile_id: str) -> bool:
        shared_deleted = await self.registry.delete(profile_id) if self.registry.configured else False
        async with self._lock:
            local_deleted = self._profiles.pop(profile_id, None) is not None
        return local_deleted or shared_deleted

    @staticmethod
    def _input_limit(kind: str) -> int:
        if kind == "rank":
            return 5_000
        if kind == "extract":
            return 200_000
        return 100_000

    async def execute(self, request: FastDecisionRequest) -> FastDecisionResponse:
        profile = self._profiles.get(request.profile_id)
        if not profile:
            profile = await self._load_shared_profile(request.profile_id)
        if not profile:
            raise FileNotFoundError(f"fast profile not found: {request.profile_id}")
        if len(request.input) > self._input_limit(profile.summary.kind):
            raise ValueError(f"input exceeds fast-profile limit for kind={profile.summary.kind}")

        async with self._state_lock:
            if self._pending >= self.queue_capacity:
                self._rejected += 1
                raise FastPathOverloaded(
                    "pending_capacity_exceeded",
                    max(0, self._pending - self._inflight),
                    self.queue_capacity,
                )
            self._pending += 1

        queued_at = time.perf_counter()
        acquired = False
        try:
            try:
                if self.max_queue_wait_ms > 0:
                    await asyncio.wait_for(self._slots.acquire(), timeout=self.max_queue_wait_ms / 1000.0)
                else:
                    await self._slots.acquire()
                acquired = True
            except TimeoutError as exc:
                async with self._state_lock:
                    self._queue_timeouts += 1
                    self._rejected += 1
                    queue_depth = max(0, self._pending - self._inflight)
                raise FastPathOverloaded("queue_wait_timeout", queue_depth, self.queue_capacity) from exc

            queue_ms = (time.perf_counter() - queued_at) * 1000.0
            async with self._state_lock:
                self._inflight += 1
            return await self._execute_direct(request, profile, queue_ms)
        finally:
            if acquired:
                self._slots.release()
            async with self._state_lock:
                if acquired:
                    self._inflight = max(0, self._inflight - 1)
                self._pending = max(0, self._pending - 1)

    async def _execute_direct(
        self,
        request: FastDecisionRequest,
        profile: CompiledProfile,
        queue_ms: float,
    ) -> FastDecisionResponse:
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

        execution_ms = (time.perf_counter() - started) * 1000.0
        latency_ms = queue_ms + execution_ms
        async with self._state_lock:
            self._completed += 1
        return FastDecisionResponse(
            request_id=request.request_id,
            profile_id=request.profile_id,
            kind=profile.summary.kind,
            ok=ok,
            data=data,
            error=error,
            provider=profile.summary.provider,
            latency_ms=round(latency_ms, 3),
            queue_ms=round(queue_ms, 3),
            execution_ms=round(execution_ms, 3),
            target_ms=profile.summary.target_ms,
            within_target=bool(ok and latency_ms <= profile.summary.target_ms),
        )
